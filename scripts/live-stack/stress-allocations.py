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
import json
import math
import random
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from kdive.mcp.dev_harness import LiveStackToolError
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
    lost: dict[str, tuple[str, dict[str, object]]] = field(default_factory=dict)
    key_ids: dict[str, str] = field(default_factory=dict)
    invalid: defaultdict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    granted: int = 0
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
        key = call.args.get("idempotency_key")
        if isinstance(key, str) and call.outcome in (OK, ENVELOPE, TOOL_ERROR):
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
                f"{tool} with key {key} got no reply, even on replay"
                for key, (tool, _) in sorted(self.lost.items())
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
    return profile


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
