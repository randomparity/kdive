#!/usr/bin/env python3
"""Hammer a running KDIVE stack with concurrent allocation clients (#2769).

Design: docs/workflow/specs/2026-09-24-stress-allocations-design.md. Run from the repository root
against a stack brought up by ``stack-services.sh`` and ``onboard.sh``::

    uv run python scripts/live-stack/stress-allocations.py --clients 8 --duration 120

The server URL and token resolve the way ``kdivectl`` resolves them. Exit status: 0 no invariant
violation, 1 at least one violation, 2 usage or preflight failure, 130 interrupted (after the
drain and the report).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import math
import random
import sys
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from kdive.cli.transport import Session
from kdive.mcp.dev_harness import LiveStackClient, LiveStackToolError
from kdive.mcp.responses import ToolResponse
from kdive.profiles.provisioning import ProvisioningProfile

OK = "ok"
ENVELOPE = "envelope-failure"
TOOL_ERROR = "tool-error"
TRANSPORT = "transport"
TIMEOUT = "timeout"
ABANDONED = "abandoned"
TERMINAL_ALLOCATION = frozenset({"released", "expired", "failed"})
LIVE_ALLOCATION = frozenset({"requested", "granted", "active"})
TERMINAL_SYSTEM = frozenset({"torn_down", "failed"})
EXIT_CLEAN, EXIT_VIOLATION, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130
# Pacing, not flags: the unit test shrinks these so the fake-stack runs stay fast.
HOLD_MAX_S = 2.0
CANCEL_MAX_S = 0.05
SAMPLE_INTERVAL_S = 1.0
DRAIN_POLL_S = 2.0
# The reconciler sweeps expired leases every 30 s; the drain must outlast one lease plus this.
SWEEP_MARGIN_S = 60.0
# A System is torn down by a worker job the reconciler enqueues after its allocation ends.
TEARDOWN_ALLOWANCE_S = 240.0
_DETAIL_LIMIT = 200
_SIZING_FIELDS = frozenset({"vcpu", "memory_mb", "disk_gb"})


class Caller(Protocol):
    async def call_tool(
        self, name: str, /, **args: object
    ) -> ToolResponse | list[ToolResponse]: ...


Connect = Callable[[], AbstractAsyncContextManager[Caller]]


class PreflightError(RuntimeError):
    """The stack cannot be exercised: unreachable, no shape, or no schedulable host."""


@dataclass(frozen=True)
class Config:
    project: str
    clients: int
    duration_s: float
    seed: int
    run_id: str
    invalid_ratio: float
    race_ratio: float
    abandon_ratio: float
    lease_h: float
    call_timeout_s: float
    drain_timeout_s: float
    profile: dict[str, Any] | None


@dataclass(frozen=True)
class Call:
    tool: str
    outcome: str
    latency_s: float
    args: dict[str, object] = field(default_factory=dict)
    response: ToolResponse | None = None
    detail: str = ""
    drain: bool = False


def _state(call: Call) -> str | None:
    """The object state a call reported: ``data.current_status`` on a failure, else ``status``."""
    if call.response is None:
        return None
    current = call.response.data.get("current_status")
    if call.response.error_category and isinstance(current, str):
        return current
    return call.response.status


@dataclass
class Ledger:
    """Every call made, every violation seen, and every object the run must settle."""

    calls: list[Call] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    owned: set[str] = field(default_factory=set)
    abandoned: set[str] = field(default_factory=set)
    systems: set[str] = field(default_factory=set)
    lost: dict[str, tuple[str, dict[str, object], bool]] = field(default_factory=dict)
    key_ids: dict[str, str] = field(default_factory=dict)
    invalid: defaultdict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    granted: int = 0
    provisioned: int = 0
    valid_errors: Counter[str] = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)

    def violate(self, invariant: int, message: str) -> None:
        self.violations.append(f"invariant {invariant}: {message}")

    def record(self, call: Call, *, invalid: bool = False, abandon: bool = False) -> None:
        self.calls.append(call)
        if invalid and call.outcome in (TRANSPORT, TIMEOUT):
            self.violate(2, f"invalid call to {call.tool} got {call.outcome}: {call.detail}")
        elif not invalid and call.outcome in (TRANSPORT, TIMEOUT, TOOL_ERROR):
            self.valid_errors[call.outcome] += 1  # counted; a stranded grant is invariant 5's
        # A valid call's tool-error may follow a commit (the handler raised after admission), so
        # its key stays for the drain to replay; an invalid call's tool-error is a binding reject.
        confirmed = call.outcome in (OK, ENVELOPE) or (invalid and call.outcome == TOOL_ERROR)
        key = call.args.get("idempotency_key")
        if isinstance(key, str) and confirmed:
            self.lost.pop(key, None)
        if call.response is not None:
            self._track(call, abandon=abandon)

    def _track(self, call: Call, *, abandon: bool) -> None:
        assert call.response is not None
        state = _state(call)
        if call.tool == "allocations.request" and call.outcome == OK:
            if state in LIVE_ALLOCATION and not call.drain:
                self.granted += 1
            # Only a deny-mode grant carries the short lease; a promoted queued row holds the
            # server's default lease, so the drain releases it instead of waiting for expiry.
            expires = abandon and call.args.get("on_capacity") == "deny" and state != "requested"
            (self.abandoned if expires else self.owned).add(call.response.object_id)
        elif call.tool == "systems.provision" and call.outcome == OK:
            system_id = call.response.data.get("system_id")
            if isinstance(system_id, str):
                self.systems.add(system_id)
            if not call.drain:
                self.provisioned += 1
        elif call.tool in ("allocations.release", "allocations.wait"):
            if state in TERMINAL_ALLOCATION:
                self.owned.discard(str(call.args.get("allocation_id")))
                self.abandoned.discard(str(call.args.get("allocation_id")))
        elif call.tool == "systems.get" and state in TERMINAL_SYSTEM:
            self.systems.discard(str(call.args.get("system_id")))

    def note_key(self, key: str, call: Call) -> None:
        if call.outcome != OK or call.response is None:
            return
        first = self.key_ids.setdefault(key, call.response.object_id)
        if first != call.response.object_id:
            self.violate(4, f"idempotency key {key} returned {first} and {call.response.object_id}")

    def leftovers(self) -> list[str]:
        """What is still unsettled; each entry is an invariant 5 violation."""
        return [
            *(f"allocation {alloc} was not released" for alloc in sorted(self.owned)),
            *(
                f"abandoned allocation {alloc} was not reclaimed by lease expiry"
                for alloc in sorted(self.abandoned)
            ),
            *(f"system {system} was not torn down" for system in sorted(self.systems)),
            *(
                f"{tool} with key {key} was never confirmed, even on replay"
                for key, (tool, _, _) in sorted(self.lost.items())
            ),
        ]


def _clip(text: str) -> str:
    return text if len(text) <= _DETAIL_LIMIT else text[: _DETAIL_LIMIT - 3] + "..."


def _describe(call: Call) -> str:
    if call.response is not None and call.response.error_category:
        return call.response.error_category
    return call.outcome


async def invoke(
    client: Caller, tool: str, args: dict[str, object], *, timeout_s: float, drain: bool = False
) -> Call:
    """Make one call and classify it into exactly one outcome class."""
    start = time.monotonic()

    def done(outcome: str, *, response: ToolResponse | None = None, detail: str = "") -> Call:
        latency = time.monotonic() - start
        return Call(tool, outcome, latency, args, response, _clip(detail), drain)

    try:
        result = await asyncio.wait_for(client.call_tool(tool, **args), timeout_s)
    except TimeoutError:
        return done(TIMEOUT, detail=f"no reply in {timeout_s:.3f}s")
    except LiveStackToolError as exc:
        return done(TOOL_ERROR, detail=exc.message)
    except Exception as exc:  # every failure other than a tool error is the transport class
        return done(TRANSPORT, detail=f"{type(exc).__name__}: {exc}")
    if not isinstance(result, ToolResponse):
        return done(TRANSPORT, detail="a list where one envelope was expected")
    return done(ENVELOPE if result.error_category else OK, response=result)


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty sequence."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1]


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def build_report(
    cfg: Config, ledger: Ledger, *, elapsed_s: float, interrupted: bool
) -> dict[str, Any]:
    outcomes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    latencies: defaultdict[str, list[float]] = defaultdict(list)
    categories: Counter[str] = Counter()
    for call in ledger.calls:
        label = f"{call.tool} (drain)" if call.drain else call.tool
        outcomes[label][call.outcome] += 1
        latencies[label].append(call.latency_s)
        if call.response is not None and call.response.error_category:
            categories[call.response.error_category] += 1
    tools = {
        label: {
            "outcomes": dict(sorted(outcomes[label].items())),
            "p50_ms": _ms(percentile(latencies[label], 50)),
            "p95_ms": _ms(percentile(latencies[label], 95)),
            "max_ms": _ms(max(latencies[label])),
        }
        for label in sorted(outcomes)
    }
    return {
        "seed": cfg.seed,
        "run_id": cfg.run_id,
        "elapsed_s": round(elapsed_s, 1),
        "interrupted": interrupted,
        "calls": len(ledger.calls),
        "granted": ledger.granted,
        "provisioning": cfg.profile is not None,
        "provisioned": ledger.provisioned,
        "valid_call_errors": dict(sorted(ledger.valid_errors.items())),
        "notes": list(ledger.notes),
        "tools": tools,
        "error_categories": dict(sorted(categories.items())),
        "invalid_calls": {
            name: dict(sorted(counts.items())) for name, counts in sorted(ledger.invalid.items())
        },
        "violations": list(ledger.violations),
    }


def _counts(counts: dict[str, int]) -> str:
    return " ".join(f"{name}={count}" for name, count in counts.items())


def render_report(report: dict[str, Any]) -> str:
    head = (
        f"seed {report['seed']}  run {report['run_id']}  elapsed {report['elapsed_s']}s  "
        f"calls {report['calls']}  granted {report['granted']}"
    )
    lines = [head + ("  (interrupted)" if report["interrupted"] else "")]
    if report["granted"] == 0:
        lines.append("warning: no allocation was granted, so most invariants were not exercised")
    if report["provisioning"] and report["provisioned"] == 0:
        lines.append("warning: --provision-profile was set but no systems.provision succeeded")
    lines.append(f"{'tool':<36} {'p50ms':>8} {'p95ms':>8} {'maxms':>8}  outcomes")
    for label, row in report["tools"].items():
        timings = f"{row['p50_ms']:>8} {row['p95_ms']:>8} {row['max_ms']:>8}"
        lines.append(f"{label:<36} {timings}  {_counts(row['outcomes'])}")
    if report["error_categories"]:
        lines.append(f"error categories: {_counts(report['error_categories'])}")
    if report["valid_call_errors"]:
        errors = _counts(report["valid_call_errors"])
        lines.append(f"valid-call errors (counted, not violations): {errors}")
    lines.extend(f"note: {note}" for note in report["notes"])
    if report["invalid_calls"]:
        lines.append("invalid calls:")
        for name, counts in report["invalid_calls"].items():
            lines.append(f"  {name:<24} {_counts(counts)}")
    lines.append(f"violations: {len(report['violations'])}")
    lines.extend(f"  {violation}" for violation in report["violations"])
    return "\n".join(lines)


def _load_profile(parser: argparse.ArgumentParser, path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
        ProvisioningProfile.model_validate(profile)
    except (OSError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
        parser.error(f"--provision-profile {path}: {exc}")
    # The server fills sizing from each allocation and rejects a restated size that differs, and
    # the script's grants come in several sizes, so the profile never restates one.
    return {name: value for name, value in profile.items() if name not in _SIZING_FIELDS}


def parse_config(argv: Sequence[str] | None = None) -> Config:
    """Parse flags; a bad value exits 2 through ``argparse``."""
    parser = argparse.ArgumentParser(
        description="Drive a running KDIVE stack from concurrent allocation clients."
    )
    parser.add_argument("--project", default="demo", help="project to allocate in")
    parser.add_argument("--clients", type=int, default=8, help="concurrent simulated clients")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds of load")
    parser.add_argument("--seed", type=int, help="RNG seed; random and printed when omitted")
    parser.add_argument("--invalid-ratio", type=float, default=0.2, help="share of invalid calls")
    parser.add_argument("--race-ratio", type=float, default=0.2, help="share of race scenarios")
    parser.add_argument("--abandon-ratio", type=float, default=0.1, help="share of abandonments")
    parser.add_argument("--lease", type=float, default=0.02, help="lease window in hours")
    parser.add_argument("--call-timeout", type=float, default=60.0, help="seconds per call")
    parser.add_argument("--drain-timeout", type=float, default=600.0, help="cleanup deadline")
    parser.add_argument("--provision-profile", type=Path, help="JSON profile; enables provision")
    args = parser.parse_args(argv)
    if args.clients < 1:
        parser.error("--clients must be at least 1")
    for flag, value in (("--duration", args.duration), ("--call-timeout", args.call_timeout)):
        if not value > 0:
            parser.error(f"{flag} must be greater than 0")
    ratios = (args.invalid_ratio, args.race_ratio, args.abandon_ratio)
    if any(not 0 <= ratio <= 1 for ratio in ratios) or sum(ratios) > 1:
        parser.error("the three --*-ratio flags must each be in [0, 1] and sum to at most 1")
    if not 0 < args.lease <= 24:
        parser.error("--lease must be in (0, 24] hours")
    floor = args.lease * 3600 + SWEEP_MARGIN_S
    if args.provision_profile is not None:
        floor += TEARDOWN_ALLOWANCE_S
    if args.drain_timeout < floor:
        parser.error(
            f"--drain-timeout must be at least {floor:.0f} s: the lease plus 60 s, plus 240 s "
            "for System teardown with --provision-profile"
        )
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    return Config(
        project=args.project,
        clients=args.clients,
        duration_s=args.duration,
        seed=seed,
        run_id=uuid.uuid4().hex[:12],
        invalid_ratio=args.invalid_ratio,
        race_ratio=args.race_ratio,
        abandon_ratio=args.abandon_ratio,
        lease_h=args.lease,
        call_timeout_s=args.call_timeout,
        drain_timeout_s=args.drain_timeout,
        profile=_load_profile(parser, args.provision_profile),
    )


def finish(cfg: Config, ledger: Ledger, *, elapsed_s: float, interrupted: bool) -> int:
    """Record what is still unsettled, print the report, and return the exit status."""
    for leftover in ledger.leftovers():
        ledger.violate(5, leftover)
    print(render_report(build_report(cfg, ledger, elapsed_s=elapsed_s, interrupted=interrupted)))
    if interrupted:
        return EXIT_INTERRUPTED
    return EXIT_VIOLATION if ledger.violations else EXIT_CLEAN


TRIPLE: dict[str, object] = {"vcpus": 1, "memory_gb": 1, "disk_gb": 10}


@dataclass(frozen=True)
class InvalidCall:
    """One malformed call. ``args`` takes (project, shape name, target allocation id)."""

    name: str
    tool: str
    args: Callable[[str, str, str], dict[str, object]]
    needs_grant: bool = False


INVALID_CALLS: tuple[InvalidCall, ...] = (
    InvalidCall("missing-project", "allocations.request", lambda p, s, t: {"shape": s}),
    InvalidCall(
        "shape-and-triple",
        "allocations.request",
        lambda p, s, t: {"project": p, "shape": s, **TRIPLE},
    ),
    InvalidCall(
        "partial-triple", "allocations.request", lambda p, s, t: {"project": p, "vcpus": 1}
    ),
    InvalidCall(
        "zero-vcpus", "allocations.request", lambda p, s, t: {"project": p, **TRIPLE, "vcpus": 0}
    ),
    InvalidCall(
        "negative-vcpus",
        "allocations.request",
        lambda p, s, t: {"project": p, **TRIPLE, "vcpus": -1},
    ),
    InvalidCall(
        "unknown-shape",
        "allocations.request",
        lambda p, s, t: {"project": p, "shape": "stress-no-such-shape"},
    ),
    InvalidCall(
        "bad-on-capacity",
        "allocations.request",
        lambda p, s, t: {"project": p, "shape": s, "on_capacity": "maybe"},
    ),
    InvalidCall(
        "zero-window",
        "allocations.request",
        lambda p, s, t: {"project": p, "shape": s, "window": 0},
    ),
    InvalidCall(
        "unreachable-project",
        "allocations.request",
        lambda p, s, t: {"project": "stress-no-such-project", "shape": s},
    ),
    InvalidCall(
        "release-not-uuid", "allocations.release", lambda p, s, t: {"allocation_id": "not-a-uuid"}
    ),
    InvalidCall("release-unknown-id", "allocations.release", lambda p, s, t: {"allocation_id": t}),
    InvalidCall(
        "renew-not-uuid",
        "allocations.renew",
        lambda p, s, t: {"allocation_id": "not-a-uuid", "extend": 1},
    ),
    InvalidCall(
        "renew-unknown-id", "allocations.renew", lambda p, s, t: {"allocation_id": t, "extend": 1}
    ),
    InvalidCall(
        "renew-negative-extend",
        "allocations.renew",
        lambda p, s, t: {"allocation_id": t, "extend": -1},
        needs_grant=True,
    ),
    InvalidCall(
        "renew-text-extend",
        "allocations.renew",
        lambda p, s, t: {"allocation_id": t, "extend": "abc"},
        needs_grant=True,
    ),
)


def _triple(data: dict[str, Any]) -> tuple[int, int, int] | None:
    vcpus, memory_mb, disk_gb = data.get("vcpus"), data.get("memory_mb"), data.get("disk_gb")
    if isinstance(vcpus, int) and isinstance(memory_mb, int) and isinstance(disk_gb, int):
        return vcpus, memory_mb, disk_gb
    return None


def sizings_from_shapes(items: Sequence[ToolResponse]) -> tuple[list[dict[str, object]], str]:
    """Every named shape, plus the smallest shape's size as a custom triple.

    Raises:
        PreflightError: ``shapes.list`` returned no named shape.
    """
    names = [item.data["name"] for item in items if isinstance(item.data.get("name"), str)]
    if not names:
        raise PreflightError("shapes.list returned no shape; seed one with shapes.set")
    sizings: list[dict[str, object]] = [{"shape": name} for name in names]
    triples = [t for item in items if (t := _triple(item.data)) is not None]
    if triples:
        vcpus, memory_mb, disk_gb = min(triples)
        sizings.append(
            {"vcpus": vcpus, "memory_gb": math.ceil(memory_mb / 1024), "disk_gb": disk_gb}
        )
    return sizings, str(names[0])


def _admits(host: ToolResponse) -> bool:
    cap = host.data.get("cap")
    return host.data.get("schedulable") is True and isinstance(cap, int) and cap > 0


@dataclass
class Seat:
    """One simulated client: its session, its RNG, and what it holds between actions."""

    client: Caller
    rng: random.Random
    last: dict[str, object] | None = None
    system: str | None = None


@dataclass
class Stress:
    """The workload, the monitor, and the drain, sharing one ledger."""

    cfg: Config
    ledger: Ledger
    sizings: list[dict[str, object]]
    shape: str
    draining: bool = False
    drain_deadline: float | None = None

    async def call(
        self,
        client: Caller,
        tool: str,
        args: dict[str, object],
        *,
        invalid: bool = False,
        abandon: bool = False,
        limit_s: float | None = None,
    ) -> Call:
        key = args.get("idempotency_key")
        if isinstance(key, str):  # kept until a reply confirms it, so the drain can replay it
            self.ledger.lost[key] = (tool, args, invalid)
        timeout_s = self.cfg.call_timeout_s if limit_s is None else limit_s
        if self.drain_deadline is not None:
            timeout_s = min(timeout_s, self.drain_left())
        call = await invoke(client, tool, args, timeout_s=timeout_s, drain=self.draining)
        if limit_s is not None and call.outcome == TIMEOUT:
            call = dataclasses.replace(call, outcome=ABANDONED)
        self.ledger.record(call, invalid=invalid, abandon=abandon)
        return call

    def drain_left(self) -> float:
        assert self.drain_deadline is not None
        return max(self.drain_deadline - time.monotonic(), 0.0)

    async def drain_call(
        self,
        client: Caller,
        tool: str,
        args: dict[str, object],
        *,
        invalid: bool = False,
        abandon: bool = False,
    ) -> bool:
        """Make one drain call inside the deadline; False once the deadline has passed."""
        if self.drain_left() <= 0:
            return False
        await self.call(client, tool, args, invalid=invalid, abandon=abandon)
        return True

    def key(self, rng: random.Random) -> str:
        return f"stress-{self.cfg.run_id}-{uuid.UUID(int=rng.getrandbits(128))}"

    def request_args(self, rng: random.Random, on_capacity: str | None = None) -> dict[str, object]:
        return {
            "project": self.cfg.project,
            **rng.choice(self.sizings),
            "on_capacity": on_capacity or rng.choice(("deny", "queue")),
            "window": self.cfg.lease_h,
            "idempotency_key": self.key(rng),
        }

    async def client_task(self, connect: Connect, index: int, deadline: float) -> None:
        rng = random.Random(f"{self.cfg.seed}:{index}")
        try:
            async with connect() as client:
                await self.client_loop(Seat(client, rng), deadline)
        except Exception as exc:  # a lost session must not abort the other clients or the drain
            self.ledger.notes.append(f"client {index} lost its session: {type(exc).__name__}")

    async def client_loop(self, seat: Seat, deadline: float) -> None:
        cfg = self.cfg
        while time.monotonic() < deadline:
            await asyncio.sleep(0)  # yield even when every call answers without suspending
            roll = seat.rng.random()
            if roll < cfg.invalid_ratio:
                await self.invalid_call(seat)
            elif roll < cfg.invalid_ratio + cfg.race_ratio:
                await self.race(seat)
            elif roll < cfg.invalid_ratio + cfg.race_ratio + cfg.abandon_ratio:
                await self.abandon(seat)
            else:
                await self.churn(seat)

    async def churn(self, seat: Seat) -> None:
        replay = seat.last is not None and seat.rng.random() < 0.25
        args = seat.last if replay and seat.last is not None else self.request_args(seat.rng)
        seat.last = args
        grant = await self.call(seat.client, "allocations.request", args)
        self.ledger.note_key(str(args["idempotency_key"]), grant)
        if grant.outcome == OK and grant.response is not None:
            await self.hold_and_release(seat, grant.response)

    async def maybe_provision(self, seat: Seat, alloc: str) -> bool:
        """Provision half the time, once the client's previous System is gone."""
        if self.cfg.profile is None or seat.rng.random() >= 0.5:
            return False
        if seat.system is not None:
            previous = await self.call(seat.client, "systems.get", {"system_id": seat.system})
            if _state(previous) not in TERMINAL_SYSTEM:
                return False
        args: dict[str, object] = {
            "allocation_id": alloc,
            "profile": self.cfg.profile,
            "idempotency_key": self.key(seat.rng),
        }
        call = await self.call(seat.client, "systems.provision", args)
        system_id = call.response.data.get("system_id") if call.response is not None else None
        if call.outcome == OK and isinstance(system_id, str):
            seat.system = system_id
        return True

    async def hold_and_release(self, seat: Seat, grant: ToolResponse) -> None:
        client, alloc = seat.client, grant.object_id
        release: dict[str, object] = {"allocation_id": alloc}
        if grant.status == "requested":  # a queued grant is released while it waits
            await self.call(client, "allocations.release", release)
            return
        if await self.maybe_provision(seat, alloc) and seat.rng.random() < 0.5:
            await self.call(client, "allocations.release", release)  # races the provision job
            return
        if seat.rng.random() < 0.5:
            renew: dict[str, object] = {"allocation_id": alloc, "extend": self.cfg.lease_h}
            await self.call(client, "allocations.renew", renew)
        await asyncio.sleep(seat.rng.uniform(0, HOLD_MAX_S))
        await self.call(client, "allocations.release", release)

    async def abandon(self, seat: Seat) -> None:
        """Walk away from a grant, or cancel the request in flight; lease expiry reclaims it."""
        args = self.request_args(seat.rng, on_capacity="deny")
        if seat.rng.random() < 0.5:
            limit_s = seat.rng.uniform(0, CANCEL_MAX_S)
            await self.call(seat.client, "allocations.request", args, abandon=True, limit_s=limit_s)
            return
        grant = await self.call(seat.client, "allocations.request", args, abandon=True)
        if grant.outcome == OK and grant.response is not None:
            await self.maybe_provision(seat, grant.response.object_id)

    async def race(self, seat: Seat) -> None:
        kind = seat.rng.choice(("double-release", "renew-vs-release", "shared-key"))
        if kind == "shared-key":
            await self.shared_key_race(seat)
            return
        client = seat.client
        grant = await self.call(client, "allocations.request", self.request_args(seat.rng))
        if grant.outcome != OK or grant.response is None:
            return
        alloc = grant.response.object_id
        release = self.call(client, "allocations.release", {"allocation_id": alloc})
        if kind == "renew-vs-release":
            renew: dict[str, object] = {"allocation_id": alloc, "extend": self.cfg.lease_h}
            await asyncio.gather(self.call(client, "allocations.renew", renew), release)
            return
        second = self.call(client, "allocations.release", {"allocation_id": alloc})
        first_call, second_call = await asyncio.gather(release, second)
        if first_call.outcome != OK or second_call.outcome != OK:
            outcomes = f"{_describe(first_call)} and {_describe(second_call)}"
            self.ledger.violate(3, f"double release of {alloc} returned {outcomes}")

    async def shared_key_race(self, seat: Seat) -> None:
        args = self.request_args(seat.rng)
        key = str(args["idempotency_key"])
        grants = await asyncio.gather(
            *(self.call(seat.client, "allocations.request", args) for _ in range(3))
        )
        for grant in grants:
            self.ledger.note_key(key, grant)
        granted = {g.response.object_id for g in grants if g.outcome == OK and g.response}
        for alloc in sorted(granted):
            await self.call(seat.client, "allocations.release", {"allocation_id": alloc})

    async def invalid_call(self, seat: Seat) -> None:
        entry = seat.rng.choice(INVALID_CALLS)
        target = str(uuid.UUID(int=seat.rng.getrandbits(128)))
        if entry.needs_grant:
            grant = await self.call(seat.client, "allocations.request", self.request_args(seat.rng))
            if grant.outcome != OK or grant.response is None:
                return
            target = grant.response.object_id
        args = entry.args(self.cfg.project, self.shape, target)
        if entry.tool == "allocations.request":  # so a wrongly accepted grant is still settled
            lease = {"window": self.cfg.lease_h, "idempotency_key": self.key(seat.rng)}
            args = {**lease, **args}
        call = await self.call(seat.client, entry.tool, args, invalid=True)
        self.ledger.invalid[entry.name][call.outcome] += 1
        if call.outcome == OK:
            self.ledger.violate(2, f"invalid call {entry.name} was accepted by {entry.tool}")
        if entry.needs_grant:
            await self.call(seat.client, "allocations.release", {"allocation_id": target})

    async def monitor(self, client: Caller, stop: asyncio.Event) -> None:
        while not stop.is_set():
            call = await self.call(client, "resources.availability", {})
            if call.outcome == OK and call.response is not None:
                for host in call.response.items:
                    self.check_host(host)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), SAMPLE_INTERVAL_S)

    def check_host(self, host: ToolResponse) -> None:
        cap, in_use = host.data.get("cap"), host.data.get("in_use")
        if host.data.get("schedulable") is not True:
            return
        if isinstance(cap, int) and isinstance(in_use, int) and in_use > cap:
            self.ledger.violate(1, f"host {host.object_id} in_use {in_use} > cap {cap}")

    async def drain(self, client: Caller) -> None:
        """Replay lost calls, release what the run owns, and wait for the rest to settle.

        Every call is bounded by ``--drain-timeout``; what the deadline cuts off stays in the
        ledger and ``finish`` reports it.
        """
        self.draining = True
        self.drain_deadline = time.monotonic() + self.cfg.drain_timeout_s
        ledger = self.ledger
        lost = list(ledger.lost.values())
        for tool, args, invalid in lost:
            if tool != "allocations.request":
                continue
            if not await self.drain_call(client, tool, args, invalid=invalid, abandon=True):
                return
        for alloc in sorted(ledger.owned):  # before any provision replay can mint a new System
            if not await self.drain_call(client, "allocations.release", {"allocation_id": alloc}):
                return
        for tool, args, invalid in lost:
            if tool == "allocations.request":
                continue
            if not await self.drain_call(client, tool, args, invalid=invalid):
                return
        while await self.poll_pass(client) and self.drain_left() > 0:
            await asyncio.sleep(min(DRAIN_POLL_S, self.drain_left()))

    async def poll_pass(self, client: Caller) -> bool:
        """Re-release, re-read and re-check once; True while something is left to settle."""
        ledger = self.ledger
        for alloc in sorted(ledger.owned):
            if not await self.drain_call(client, "allocations.release", {"allocation_id": alloc}):
                return False
        for alloc in sorted(ledger.abandoned):
            wait: dict[str, object] = {"allocation_id": alloc, "timeout_s": 0}
            if not await self.drain_call(client, "allocations.wait", wait):
                return False
        for system in sorted(ledger.systems):
            if not await self.drain_call(client, "systems.get", {"system_id": system}):
                return False
        return bool(ledger.owned or ledger.abandoned or ledger.systems)


async def _preflight(cfg: Config, probe: Caller) -> tuple[list[dict[str, object]], str]:
    shapes = await invoke(probe, "shapes.list", {}, timeout_s=cfg.call_timeout_s)
    if shapes.outcome != OK or shapes.response is None:
        raise PreflightError(f"shapes.list failed: {_describe(shapes)} {shapes.detail}")
    hosts = await invoke(probe, "resources.availability", {}, timeout_s=cfg.call_timeout_s)
    if hosts.outcome != OK or hosts.response is None:
        raise PreflightError(f"resources.availability failed: {_describe(hosts)} {hosts.detail}")
    if not any(_admits(host) for host in hosts.response.items):
        raise PreflightError("no schedulable host with cap above 0; register one with onboard.sh")
    return sizings_from_shapes(shapes.response.items)


async def run_stress(cfg: Config, connect: Connect, ledger: Ledger) -> None:
    """Preflight, run the clients and the monitor until the deadline, then drain.

    The drain also runs when the load is cancelled (Ctrl-C); a cancel during the drain stops it,
    and ``finish`` reports what was left.

    Raises:
        PreflightError: the stack is unreachable, has no shape, or has no schedulable host.
    """
    async with contextlib.AsyncExitStack() as stack:
        try:
            probe = await stack.enter_async_context(connect())
        except Exception as exc:
            raise PreflightError(f"cannot reach the stack: {type(exc).__name__}: {exc}") from exc
        sizings, shape = await _preflight(cfg, probe)
        stress = Stress(cfg, ledger, sizings, shape)
        stop = asyncio.Event()
        monitor = asyncio.create_task(stress.monitor(probe, stop))
        deadline = time.monotonic() + cfg.duration_s
        try:
            async with asyncio.TaskGroup() as group:
                for index in range(cfg.clients):
                    group.create_task(stress.client_task(connect, index, deadline))
        finally:
            stop.set()
            try:
                await stress.drain(probe)
            finally:
                await monitor


def main(argv: Sequence[str] | None = None) -> int:
    cfg = parse_config(argv)
    try:
        session = Session.from_env()
    except SystemExit as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"seed {cfg.seed}  run {cfg.run_id}", flush=True)
    ledger = Ledger()
    started = time.monotonic()
    interrupted = False
    try:
        asyncio.run(
            run_stress(cfg, lambda: LiveStackClient.over_http(session.url, session.token), ledger)
        )
    except PreflightError as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        interrupted = True
    return finish(cfg, ledger, elapsed_s=time.monotonic() - started, interrupted=interrupted)


if __name__ == "__main__":
    sys.exit(main())
