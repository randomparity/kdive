"""Unit and fake-stack tests for scripts/live-stack/stress-allocations.py (#2769)."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import LiveStackToolError
from kdive.mcp.responses import ToolResponse

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-stack" / "stress-allocations.py"
_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 10,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git#v7.0",
    "provider": {
        "local-libvirt": {
            "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/x.qcow2"},
            "crashkernel": "256M",
        }
    },
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stress_allocations", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


stress = _load()


def _config(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "project": "demo",
        "clients": 4,
        "duration_s": 0.2,
        "seed": 7,
        "run_id": "test",
        "invalid_ratio": 0.2,
        "race_ratio": 0.2,
        "abandon_ratio": 0.2,
        "lease_h": 0.02 / 3600,  # 20 ms, so abandoned grants expire inside the drain
        "call_timeout_s": 5.0,
        "drain_timeout_s": 0.5,
        "profile": None,
    }
    base.update(overrides)
    return stress.Config(**base)


class _Stub:
    def __init__(self, behavior: Any) -> None:
        self.behavior = behavior

    async def call_tool(self, name: str, /, **args: object) -> Any:
        return await self.behavior()


async def _success() -> ToolResponse:
    return ToolResponse.success("a", "granted")


async def _failure() -> ToolResponse:
    return ToolResponse.failure("a", ErrorCategory.ALLOCATION_DENIED)


async def _tool_error() -> ToolResponse:
    raise LiveStackToolError("allocations.request", "1 validation error")


async def _transport() -> ToolResponse:
    raise ConnectionError("refused")


async def _slow() -> ToolResponse:
    await asyncio.sleep(1)
    return ToolResponse.success("a", "granted")


async def _list() -> list[ToolResponse]:
    return [ToolResponse.success("a", "granted")]


@pytest.mark.parametrize(
    ("behavior", "outcome"),
    [
        (_success, "ok"),
        (_failure, "envelope-failure"),
        (_tool_error, "tool-error"),
        (_transport, "transport"),
        (_slow, "timeout"),
        (_list, "transport"),
    ],
)
def test_invoke_classifies_each_outcome(behavior: Any, outcome: str) -> None:
    call = asyncio.run(stress.invoke(_Stub(behavior), "t", {}, timeout_s=0.05))
    assert call.outcome == outcome


def _call(tool: str, outcome: str, response: ToolResponse | None = None, **args: object) -> Any:
    return stress.Call(tool, outcome, 0.01, args, response, "d")


def test_ledger_records_violations_and_settles_objects() -> None:
    ledger = stress.Ledger()
    ledger.lost["k1"] = ("allocations.request", {"idempotency_key": "k1"}, False)
    granted = ToolResponse.success("a1", "granted")
    ledger.record(_call("allocations.request", "ok", granted, idempotency_key="k1"))
    deny = {"on_capacity": "deny"}
    ledger.record(
        _call("allocations.request", "ok", ToolResponse.success("a2", "granted"), **deny),
        abandon=True,
    )
    promoted = ToolResponse.success("a4", "granted")  # a queued row the server promoted
    ledger.record(_call("allocations.request", "ok", promoted, on_capacity="queue"), abandon=True)
    replayed = stress.Call(
        "allocations.request", "ok", 0.01, {}, ToolResponse.success("a5", "expired"), drain=True
    )
    ledger.record(replayed, abandon=True)
    assert {"a4", "a5"} <= ledger.owned, "a queue-mode or drain replay is released, not left"
    ledger.owned -= {"a4", "a5"}
    provision = ToolResponse.success("j1", "running", data={"system_id": "s1"})
    ledger.record(_call("systems.provision", "ok", provision))
    ledger.record(_call("allocations.release", "timeout", allocation_id="a1"))
    ledger.lost["k2"] = ("allocations.request", {"idempotency_key": "k2"}, False)
    ledger.lost["k3"] = ("allocations.request", {"idempotency_key": "k3"}, True)
    ledger.record(_call("allocations.request", "tool-error", idempotency_key="k2"))
    ledger.record(_call("allocations.request", "tool-error", idempotency_key="k3"), invalid=True)
    assert list(ledger.lost) == ["k2"], "only an invalid call's tool-error confirms its key"
    del ledger.lost["k2"]
    ledger.record(_call("allocations.renew", "timeout"), invalid=True)
    assert ledger.valid_errors == {"timeout": 1, "tool-error": 1}
    assert (ledger.owned, ledger.abandoned, ledger.systems) == ({"a1"}, {"a2"}, {"s1"})
    assert ledger.lost == {}
    assert ledger.granted == 3  # a1, a2, a4; the drain replay of a5 is not a grant
    stale = ToolResponse.failure(
        "a2", ErrorCategory.STALE_HANDLE, data={"current_status": "expired"}
    )
    ledger.record(
        _call(
            "allocations.release", "ok", ToolResponse.success("a1", "released"), allocation_id="a1"
        )
    )
    ledger.record(_call("allocations.wait", "envelope-failure", stale, allocation_id="a2"))
    ledger.record(_call("systems.get", "ok", ToolResponse.success("s1", "ready"), system_id="s1"))
    assert (ledger.owned, ledger.abandoned, ledger.systems) == (set(), set(), {"s1"})
    ledger.note_key("k", _call("allocations.request", "ok", ToolResponse.success("a1", "granted")))
    ledger.note_key("k", _call("allocations.request", "ok", ToolResponse.success("a3", "granted")))
    assert [v.split(":")[0] for v in ledger.violations] == ["invariant 2", "invariant 4"]
    assert ledger.leftovers() == ["system s1 was not torn down"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--clients", "0"],
        ["--duration", "0"],
        ["--call-timeout", "-1"],
        ["--invalid-ratio", "0.7", "--race-ratio", "0.5"],
        ["--abandon-ratio", "0.5", "--invalid-ratio", "0.6"],
        ["--race-ratio", "1.5"],
        ["--lease", "0"],
        ["--lease", "25"],
        ["--lease", "0.1", "--drain-timeout", "100"],
        ["--provision-profile", "/nonexistent/profile.json"],
    ],
)
def test_parse_config_rejects(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        stress.parse_config(argv)
    assert excinfo.value.code == 2


@pytest.mark.parametrize("content", ["[]", "{not json", '{"schema_version": 1}'])
def test_parse_config_rejects_invalid_profile(tmp_path: Path, content: str) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        stress.parse_config(["--provision-profile", str(profile)])
    assert excinfo.value.code == 2


def test_drain_floor_covers_teardown_when_provisioning(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(_PROFILE), encoding="utf-8")
    assert stress.parse_config(["--drain-timeout", "200"]).drain_timeout_s == 200
    with pytest.raises(SystemExit) as excinfo:
        stress.parse_config(["--drain-timeout", "200", "--provision-profile", str(profile)])
    assert excinfo.value.code == 2


def test_parse_config_defaults(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(_PROFILE), encoding="utf-8")
    first = stress.parse_config(["--provision-profile", str(profile), "--seed", "7"])
    second = stress.parse_config(["--seed", "7"])
    sizing = {"vcpu", "memory_mb", "disk_gb"}
    assert first.profile == {k: v for k, v in _PROFILE.items() if k not in sizing}
    assert (first.project, first.clients, first.lease_h, first.seed) == ("demo", 8, 0.02, 7)
    assert first.run_id != second.run_id, "a re-run of a seed must not reuse its keys"
    assert isinstance(stress.parse_config([]).seed, int)


def test_percentile_nearest_rank() -> None:
    assert stress.percentile([4.0, 1.0, 3.0, 2.0], 50) == 2.0
    assert stress.percentile([4.0, 1.0, 3.0, 2.0], 95) == 4.0
    assert stress.percentile([5.0], 50) == 5.0


def test_report_counts_and_renders(capsys: pytest.CaptureFixture[str]) -> None:
    ledger = stress.Ledger()
    ledger.record(_call("allocations.request", "ok", ToolResponse.success("a1", "granted")))
    denied = ToolResponse.failure("r", ErrorCategory.ALLOCATION_DENIED)
    ledger.record(_call("allocations.request", "envelope-failure", denied))
    ledger.record(
        stress.Call(
            "allocations.release",
            "ok",
            0.01,
            {"allocation_id": "a1"},
            ToolResponse.success("a1", "released"),
            drain=True,
        )
    )
    ledger.invalid["zero-vcpus"]["tool-error"] += 1
    ledger.record(_call("allocations.renew", "timeout"))
    ledger.notes.append("client 3 lost its session: ConnectionError")
    ledger.violate(1, "host h in_use 3 > cap 2")
    report = stress.build_report(_config(), ledger, elapsed_s=1.0, interrupted=False)
    assert report["tools"]["allocations.request"]["outcomes"] == {"envelope-failure": 1, "ok": 1}
    assert report["tools"]["allocations.release (drain)"]["outcomes"] == {"ok": 1}
    assert report["error_categories"] == {"allocation_denied": 1}
    assert report["invalid_calls"] == {"zero-vcpus": {"tool-error": 1}}
    assert stress.finish(_config(), ledger, elapsed_s=1.0, interrupted=False) == 1
    printed = capsys.readouterr().out
    assert "run test" in printed
    assert "invariant 1: host h in_use 3 > cap 2" in printed
    assert "valid-call errors (counted, not violations): timeout=1" in printed
    assert "note: client 3 lost its session: ConnectionError" in printed
    assert len(ledger.violations) == 1
    assert "warning: no allocation was granted" not in printed
    assert stress.finish(_config(), stress.Ledger(), elapsed_s=0, interrupted=True) == 130
    assert "warning: no allocation was granted" in capsys.readouterr().out
    stress.finish(_config(profile={"x": 1}), stress.Ledger(), elapsed_s=0, interrupted=False)
    assert "no systems.provision succeeded" in capsys.readouterr().out


# --- Task 2: the driver against an in-memory fake stack ---------------------------------------

_SHAPES = [
    ToolResponse.success(
        "small", "ok", data={"name": "small", "vcpus": 1, "memory_mb": 1024, "disk_gb": 10}
    )
]
_OCCUPYING = ("granted", "active")


class FakeStack:
    """Just enough allocation semantics to exercise every invariant; one flag plants each defect."""

    def __init__(
        self,
        *,
        defect: str | None = None,
        shapes: list[ToolResponse] | None = None,
        hosts: bool = True,
        hang: bool = False,
    ) -> None:
        self.defect = defect
        self.shapes = _SHAPES if shapes is None else shapes
        self.hosts = hosts
        self.hang = hang
        self.stall = False
        self.hung = asyncio.Event()
        self.hung_id: str | None = None
        self.cap = 2
        self.states: dict[str, str] = {}
        self.expiry: dict[str, float] = {}
        self.keys: dict[str, str] = {}
        self.provisions: dict[str, ToolResponse] = {}
        self.systems: dict[str, str] = {}
        self.tearing_down: set[str] = set()
        self.counter = 0
        self.requests: list[dict[str, Any]] = []

    def connect(self) -> FakeClient:
        return FakeClient(self)

    def _new_id(self) -> str:
        self.counter += 1
        return str(uuid.UUID(int=self.counter))

    def _occupied(self) -> int:
        return sum(state in _OCCUPYING for state in self.states.values())

    def _sweep(self) -> None:
        if self.defect == "no_gc":
            return
        now = time.monotonic()
        for alloc, expires in self.expiry.items():
            if self.states[alloc] in _OCCUPYING and expires < now:
                self.states[alloc] = "expired"
        for alloc, state in self.states.items():
            if state == "requested" and self._occupied() < self.cap:
                self.states[alloc] = "granted"  # a promoted row takes the server's 4 h default
                self.expiry[alloc] = now + 4 * 3600

    def handle(self, name: str, args: dict[str, Any]) -> ToolResponse:
        self._sweep()
        handlers = {
            "shapes.list": self._shapes,
            "resources.availability": self._availability,
            "allocations.request": self._request,
            "allocations.release": self._release,
            "allocations.renew": self._renew,
            "allocations.wait": self._wait,
            "systems.provision": self._provision,
            "systems.get": self._system,
        }
        return handlers[name](args)

    def _shapes(self, args: dict[str, Any]) -> ToolResponse:
        return ToolResponse.collection("shapes", "ok", list(self.shapes))

    def _availability(self, args: dict[str, Any]) -> ToolResponse:
        in_use = self.cap + 1 if self.defect == "overshoot" else self._occupied()
        cordoned = ToolResponse.success(
            "h2", "cordoned", data={"schedulable": False, "cap": self.cap, "in_use": self.cap + 1}
        )
        host = ToolResponse.success(
            "h1", "available", data={"schedulable": True, "cap": self.cap, "in_use": in_use}
        )
        return ToolResponse.collection(
            "resources", "ok", [host, cordoned] if self.hosts else [cordoned]
        )

    def _invalid_request(self, args: dict[str, Any]) -> bool:
        triple = [args[k] for k in ("vcpus", "memory_gb", "disk_gb") if k in args]
        has_shape = "shape" in args
        return (
            has_shape == bool(triple)
            or (bool(triple) and len(triple) != 3)
            or any(value <= 0 for value in triple)
            or (has_shape and args["shape"] != "small")
            or args["project"] != "demo"
        )

    def _request(self, args: dict[str, Any]) -> ToolResponse:
        self.requests.append(args)
        accept = self.defect == "accept_invalid"
        binding_error = (
            "project" not in args
            or args.get("on_capacity", "deny") not in ("deny", "queue")
            or args.get("window") == 0
        )
        if binding_error and not accept:
            raise LiveStackToolError("allocations.request", "1 validation error")
        if not accept and not binding_error and self._invalid_request(args):
            return ToolResponse.failure("r", ErrorCategory.CONFIGURATION_ERROR)
        key = args.get("idempotency_key")
        if isinstance(key, str) and key in self.keys and self.defect != "split_keys":
            alloc = self.keys[key]
            return ToolResponse.success(alloc, self.states[alloc])
        if self._occupied() >= self.cap and not accept:
            if args.get("on_capacity") != "queue":
                return ToolResponse.failure("h1", ErrorCategory.ALLOCATION_DENIED)
            state = "requested"
        else:
            state = "granted"
        alloc = self._new_id()
        self.states[alloc] = state
        if state == "granted" and args.get("window"):
            self.expiry[alloc] = time.monotonic() + float(args["window"]) * 3600
        if isinstance(key, str):
            self.keys[key] = alloc
            if self.hang:  # commit, then never answer this one call
                self.hang, self.stall, self.hung_id = False, True, alloc
        return ToolResponse.success(alloc, state)

    def _release(self, args: dict[str, Any]) -> ToolResponse:
        alloc = args["allocation_id"]
        if alloc not in self.states:
            if self.defect == "accept_invalid":
                return ToolResponse.success(alloc, "released")
            return ToolResponse.failure(alloc, ErrorCategory.CONFIGURATION_ERROR)
        if self.defect == "stuck_release":
            return ToolResponse.failure(alloc, ErrorCategory.INFRASTRUCTURE_FAILURE)
        if self.states[alloc] in ("expired", "failed"):
            current = {"current_status": self.states[alloc]}
            return ToolResponse.failure(alloc, ErrorCategory.STALE_HANDLE, data=current)
        self.states[alloc] = "released"
        return ToolResponse.success(alloc, "released")

    def _renew(self, args: dict[str, Any]) -> ToolResponse:
        alloc = args["allocation_id"]
        if self.defect == "accept_invalid":
            return ToolResponse.success(alloc, "granted")
        try:
            extend = float(args["extend"])
        except ValueError:
            extend = 0.0
        if alloc not in self.states or extend <= 0:
            return ToolResponse.failure(alloc, ErrorCategory.CONFIGURATION_ERROR)
        if self.states[alloc] not in _OCCUPYING:
            return ToolResponse.failure(alloc, ErrorCategory.STALE_HANDLE)
        self.expiry[alloc] = time.monotonic() + extend * 3600
        return ToolResponse.success(alloc, self.states[alloc])

    def _wait(self, args: dict[str, Any]) -> ToolResponse:
        alloc = args["allocation_id"]
        return ToolResponse.success(alloc, self.states[alloc])

    def _provision(self, args: dict[str, Any]) -> ToolResponse:
        key, alloc = args["idempotency_key"], args["allocation_id"]
        if key in self.provisions:
            return self.provisions[key]
        if self.states.get(alloc) != "granted":
            return ToolResponse.failure(alloc, ErrorCategory.STALE_HANDLE)
        small = {"vcpu": 1, "memory_mb": 1024, "disk_gb": 10}  # every fake grant is `small`
        if any(args["profile"].get(name, size) != size for name, size in small.items()):
            return ToolResponse.failure(alloc, ErrorCategory.CONFIGURATION_ERROR)
        self.states[alloc] = "active"
        system = self._new_id()
        self.systems[system] = alloc
        self.provisions[key] = ToolResponse.success(
            self._new_id(), "running", data={"system_id": system}
        )
        return self.provisions[key]

    def _system(self, args: dict[str, Any]) -> ToolResponse:
        system = args["system_id"]
        if self.states[self.systems[system]] in _OCCUPYING:
            return ToolResponse.success(system, "ready")
        if system in self.tearing_down:  # the teardown job ran on a later pass
            return ToolResponse.success(system, "torn_down")
        self.tearing_down.add(system)
        return ToolResponse.success(system, "tearing_down")


class FakeClient:
    def __init__(self, stack: FakeStack) -> None:
        self.stack = stack

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, /, **args: object) -> ToolResponse:
        response = self.stack.handle(name, dict(args))
        if self.stack.stall:
            self.stack.stall = False
            self.stack.hung.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)  # the reply can be lost after the commit, as on a real server
        return response


@pytest.fixture(autouse=True)
def _fast_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stress, "HOLD_MAX_S", 0.005)
    monkeypatch.setattr(stress, "CANCEL_MAX_S", 0.001)
    monkeypatch.setattr(stress, "SAMPLE_INTERVAL_S", 0.02)
    monkeypatch.setattr(stress, "DRAIN_POLL_S", 0.01)


def _run(stack: FakeStack, cfg: Any) -> tuple[int, Any]:
    ledger = stress.Ledger()
    asyncio.run(stress.run_stress(cfg, stack.connect, ledger))
    return stress.finish(cfg, ledger, elapsed_s=cfg.duration_s, interrupted=False), ledger


def _settled(stack: FakeStack) -> bool:
    return all(state in ("released", "expired") for state in stack.states.values())


@pytest.mark.parametrize("provision", [False, True])
def test_clean_stack_exits_zero(provision: bool, tmp_path: Path) -> None:
    profile = None
    if provision:  # through parse_config, as the operator's file would go
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(_PROFILE), encoding="utf-8")
        profile = stress.parse_config(["--provision-profile", str(path)]).profile
    stack = FakeStack()
    code, ledger = _run(stack, _config(profile=profile))
    assert ledger.violations == []
    assert code == 0
    outcomes = {call.outcome for call in ledger.calls}
    tools = {call.tool for call in ledger.calls}
    assert {"allocations.request", "allocations.release", "resources.availability"} <= tools
    assert ledger.invalid, "the invalid catalog was never exercised"
    assert "abandoned" in outcomes or "expired" in stack.states.values(), "nothing was abandoned"
    assert _settled(stack)
    assert all("idempotency_key" in args for args in stack.requests)
    assert all("window" in args for args in stack.requests), "a request without a lease"
    if profile is not None:
        assert stack.systems, "no System was provisioned"


@pytest.mark.parametrize(
    ("defect", "overrides", "expected"),
    [
        ("overshoot", {"invalid_ratio": 0.0, "race_ratio": 0.0}, {"invariant 1"}),
        (
            "accept_invalid",
            {"invalid_ratio": 1.0, "race_ratio": 0.0, "abandon_ratio": 0.0},
            {"invariant 2"},
        ),
        ("split_keys", {"invalid_ratio": 0.0, "race_ratio": 0.8}, {"invariant 4"}),
        (
            "stuck_release",
            {"invalid_ratio": 0.0, "race_ratio": 0.8},
            {"invariant 3", "invariant 5"},
        ),
        ("no_gc", {"invalid_ratio": 0.0, "race_ratio": 0.0, "abandon_ratio": 1.0}, {"invariant 5"}),
    ],
)
def test_planted_defect_is_reported(
    defect: str, overrides: dict[str, Any], expected: set[str]
) -> None:
    code, ledger = _run(FakeStack(defect=defect), _config(**overrides))
    assert code == 1
    assert expected <= {violation.split(":")[0] for violation in ledger.violations}


def test_cancelled_run_replays_the_lost_request() -> None:
    stack = FakeStack(hang=True)
    cfg = _config(duration_s=30.0, invalid_ratio=0.0, race_ratio=0.0, abandon_ratio=0.0)
    ledger = stress.Ledger()

    async def interrupted_run() -> None:
        task = asyncio.create_task(stress.run_stress(cfg, stack.connect, ledger))
        await stack.hung.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(interrupted_run())
    assert stress.finish(cfg, ledger, elapsed_s=0.1, interrupted=True) == 130
    assert ledger.violations == []
    assert stack.hung_id is not None
    replays = [c for c in ledger.calls if c.drain and c.tool == "allocations.request"]
    assert [c.response.object_id for c in replays if c.response] == [stack.hung_id]
    assert stack.states[stack.hung_id] in ("released", "expired")
    assert _settled(stack)


def test_cancel_during_drain_reports_leftovers(capsys: pytest.CaptureFixture[str]) -> None:
    stack = FakeStack(defect="no_gc")
    cfg = _config(duration_s=0.05, invalid_ratio=0.0, race_ratio=0.0, abandon_ratio=1.0)
    cfg = dataclasses.replace(cfg, drain_timeout_s=30.0)
    ledger = stress.Ledger()

    async def interrupted_drain() -> None:
        task = asyncio.create_task(stress.run_stress(cfg, stack.connect, ledger))
        while not any(call.drain for call in ledger.calls):
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(interrupted_drain())
    assert stress.finish(cfg, ledger, elapsed_s=0.1, interrupted=True) == 130
    assert any("not reclaimed by lease expiry" in v for v in ledger.violations)
    assert "(interrupted)" in capsys.readouterr().out


def test_drain_stops_at_its_deadline() -> None:
    async def hang() -> ToolResponse:
        await asyncio.sleep(30)
        return ToolResponse.success("a", "released")

    cfg = _config(call_timeout_s=5.0, drain_timeout_s=0.3)
    ledger = stress.Ledger()
    ledger.owned.update({"a1", "a2", "a3"})
    ledger.lost["k"] = ("allocations.request", {"idempotency_key": "k"}, False)
    started = time.monotonic()
    asyncio.run(stress.Stress(cfg, ledger, [], "small").drain(_Stub(hang)))
    assert time.monotonic() - started < 1.0
    assert len(ledger.leftovers()) == 4


@pytest.mark.parametrize(
    ("stack", "match"),
    [(FakeStack(shapes=[]), "no shape"), (FakeStack(hosts=False), "no schedulable host")],
)
def test_preflight_failure(stack: FakeStack, match: str) -> None:
    with pytest.raises(stress.PreflightError, match=match):
        asyncio.run(stress.run_stress(_config(), stack.connect, stress.Ledger()))
