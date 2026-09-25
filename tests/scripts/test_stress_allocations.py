"""Unit and fake-stack tests for scripts/live-stack/stress-allocations.py (#2769)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
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
    ledger.lost["k1"] = ("allocations.request", {"idempotency_key": "k1"})
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
    ledger.record(_call("allocations.request", "tool-error"))
    ledger.record(_call("allocations.request", "tool-error"), invalid=True)
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
    assert first.profile == _PROFILE
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
