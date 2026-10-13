"""Unit tests of the local power and snapshot cell oracles and rejections (no stack needed)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import build_contract
from tests.integration.live_stack import power_snapshots
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.power_snapshots import (
    STATE_PROBE,
    GuestState,
    Session,
    rejection,
    require_disk_rollback,
    require_restored,
    seed_command,
)
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.tool_cells import Grants, Guest, HttpCaller, LaneTarget

_CHECKPOINT = GuestState("boot-a", "d0", "r0", "41:900")
_MUTATED = GuestState("boot-a", "d1", "r1", "41:900")


def test_state_probe_parses_the_four_facts() -> None:
    state = GuestState.parse("boot_id=b\ndisk=d\nram=r\nprocess=7:12\n")
    assert state == GuestState("b", "d", "r", "7:12")
    assert "drop_caches" in STATE_PROBE.split("printf")[0]


def test_memory_restore_passes_only_on_the_checkpoint() -> None:
    require_restored(_CHECKPOINT, _MUTATED, _CHECKPOINT)


@pytest.mark.parametrize(
    ("restored", "missing"),
    [
        (_MUTATED, "['disk', 'ram']"),  # a no-op restore
        (GuestState("boot-b", "d0", "", ":"), "['boot_id', 'ram', 'process']"),  # a reboot
        (GuestState("boot-a", "d1", "r0", "41:900"), "['disk']"),  # RAM reverted, disk not
        (GuestState("boot-a", "d0", "r0", "41:901"), "['process']"),  # a new process
    ],
    ids=["no-op", "reboot", "ram-only", "process"],
)
def test_memory_restore_fails_each_partial_restore(restored: GuestState, missing: str) -> None:
    with pytest.raises(AssertionError, match=missing.replace("[", r"\[").replace("]", r"\]")):
        require_restored(_CHECKPOINT, _MUTATED, restored)


def test_memory_restore_needs_a_real_mutation_and_sentinel() -> None:
    with pytest.raises(AssertionError, match="did not change both"):
        require_restored(_CHECKPOINT, _CHECKPOINT, _CHECKPOINT)
    with pytest.raises(AssertionError, match="no live sentinel"):
        dead = GuestState("boot-a", "d0", "r0", ":")
        require_restored(dead, _MUTATED, dead)


def test_disk_rollback_needs_the_old_disk_in_a_new_boot() -> None:
    require_disk_rollback(_CHECKPOINT, _MUTATED, GuestState("boot-b", "d0", "", ":"))
    with pytest.raises(AssertionError, match="did not roll the disk back"):
        require_disk_rollback(_CHECKPOINT, _MUTATED, GuestState("boot-b", "d1", "", ":"))
    with pytest.raises(AssertionError, match="did not boot"):
        require_disk_rollback(_CHECKPOINT, _MUTATED, GuestState("boot-a", "d0", "r1", "41:900"))


def test_seed_refuses_a_value_that_is_not_a_token() -> None:
    assert "setsid sleep infinity" in seed_command("ab12", "cd34")
    with pytest.raises(AssertionError, match="hex tokens"):
        seed_command("a; reboot", "cd34")


_TARGET = LaneTarget("cov-t", "alloc-1", "11111111-1111-1111-1111-111111111111", {}, ())


@pytest.mark.parametrize("tool", power_snapshots.TOOLS)
def test_rejections_aim_at_the_target_with_the_gate_below(tool: str) -> None:
    gate = power_snapshots.GATES[tool]
    auth = rejection(tool, "authentication", _TARGET)
    assert auth.args["system_id"] == _TARGET.system_id
    assert auth.grants.roles == {"cov-t": "viewer"}
    below = rejection(tool, "authorization", _TARGET).grants.roles
    assert below == ({} if gate == "viewer" else {"cov-t": "viewer"})
    invalid = rejection(tool, "validation", _TARGET)
    assert invalid.args["system_id"] == 7 and invalid.grants.roles == {"cov-t": gate}
    isolated = rejection(tool, "project-isolation", _TARGET)
    assert "cov-t" not in isolated.grants.projects
    assert isolated.absent_twin is not None
    assert isolated.absent_twin.keys() == isolated.args.keys()
    assert isolated.absent_twin["system_id"] != _TARGET.system_id
    category = "configuration_error" if tool == "control.power" else "not_found"
    assert isolated.categories == {category}


def test_no_capability_rejection_cell_is_carried() -> None:
    cells = [
        c
        for c in build_contract().cells
        if c.operation in power_snapshots.TOOLS and c.provider == "local-libvirt"
    ]
    boundaries = {c.scenario_id.rsplit("/", 1)[1] for c in cells if c.kind == "rejection"}
    assert boundaries == {"authentication", "authorization", "project-isolation", "validation"}
    with pytest.raises(AssertionError, match="no capability cell"):
        rejection("systems.snapshot", "capability", _TARGET)


@dataclass
class _Caller:
    calls: list[str] = field(default_factory=list)

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse:
        self.calls.append(tool)
        return ToolResponse.success("job-1", "queued")


@dataclass
class _Op:
    calls: list[str] = field(default_factory=list)


def test_only_the_tool_under_test_uses_the_exposure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = next(
        c
        for c in build_contract().cells
        if c.operation == "systems.restore" and c.kind == "functional"
    )
    op = _Op()

    async def scalar(client: _Op, tool: str, **_args: object) -> ToolResponse:
        client.calls.append(tool)
        return ToolResponse.success("job-2", "queued")

    monkeypatch.setattr(power_snapshots, "scalar", scalar)
    caller = _Caller()
    guest = type("G", (), {"op": op, "system_id": "s"})()
    session = Session(
        CellRun(cell, EvidenceWriter(tmp_path)),
        cast(Guest, guest),
        cast(HttpCaller, caller),
        "token",
        power_snapshots.Endpoint("h", 22),
    )
    asyncio.run(session.call("systems.restore", system_id="s", name="n"))
    asyncio.run(session.call("systems.snapshot", system_id="s", name="n"))
    assert caller.calls == ["systems.restore"]
    assert op.calls == ["systems.snapshot"]


def test_protected_state_adds_the_target_jobs_and_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def project_state(_db: str, project: str) -> dict[str, list[object]]:
        seen.append(project)
        return {"snapshots": [0, "x"]}

    async def system_jobs(_db: str, system_id: str) -> list[object]:
        seen.append(system_id)
        return [2, "y"]

    monkeypatch.setattr(power_snapshots, "project_state", project_state)
    monkeypatch.setattr(power_snapshots, "system_jobs", system_jobs)
    monkeypatch.setattr(power_snapshots, "domain_defined", lambda system_id: False)
    state = asyncio.run(power_snapshots.protected_state("db", _TARGET))
    assert state == {"project": {"snapshots": [0, "x"]}, "jobs": [2, "y"], "domain_defined": False}
    assert seen == ["cov-t", _TARGET.system_id]


def test_grants_without_a_role_are_membership_only() -> None:
    assert power_snapshots.grants("p", None) == Grants("p-member", ("p",), {})
