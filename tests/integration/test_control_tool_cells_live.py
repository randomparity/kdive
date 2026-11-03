"""Prove the x86_64 traffic capture, SysRq and crash-watch tool cells over HTTP (#3115, #3116).

``live_stack``-marked (ADR-0722). One parameter per native local-libvirt contract cell and per
x86_64 remote-libvirt contract cell of ``control.capture_traffic``, ``control.diagnostic_sysrq``
and ``control.watch_for_crash``, framed by
:func:`~tests.integration.live_stack.tool_cells.run_tool_cell`; the scenarios are in
:mod:`tests.integration.live_stack.control_cells`. A local rejection cell aims at the capture
carrier's target, a remote one at :func:`~tests.integration.live_stack.control_cells.run_target`.
The ppc64le cells are #2818's. ``docs/operating/runbooks/live-testing.md`` covers the local run,
``docs/operating/runbooks/remote-live-stack.md`` the remote one.
"""

from __future__ import annotations

import os
import platform
import secrets
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.mcp.dev_harness import OidcIssuer
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.capture_cells import capture_target, snapshot_of
from tests.integration.live_stack.control_cells import (
    TOOLS,
    functional,
    rejection,
    run_target,
    target_state,
)
from tests.integration.live_stack.deep_lifecycle import FIXTURE_ROOT_ENV, load_fixture
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import (
    REMOTE_LANE_FAMILIES,
    Exposure,
    HttpCaller,
    boundary_of,
    lane_for,
    on_lane_system,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

# The kernel fixture every functional cell and the rejection target boot; write the bindings
# with ``--kernel-baseline longterm``.
BASELINE = "longterm"


def _fixture(arch: str) -> tuple[Path, dict[str, Any]]:
    """The verified ``longterm`` fixture; ``blocked`` before any stack mutation when absent."""
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the {BASELINE} fixture"
        )
    try:
        return load_fixture(Path(root), BASELINE, arch)
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{BASELINE} fixture: {exc}") from None


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    cell = run.cell
    caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
    fixture = partial(_fixture, str(cell.guest_arch))
    if cell.kind == "rejection":
        boundary = boundary_of(cell)
        if cell.provider == "remote-libvirt":
            target = await run_target(run, base_url, issuer, db_url, fixture)
            snapshot = target_state(db_url, target, lane_for(cell).connect)
        else:
            target = await capture_target(run, base_url, issuer, db_url, fixture)
            snapshot = snapshot_of(db_url, target)
        await prove_rejection(
            run, caller, boundary, rejection(cell.operation, boundary, target), snapshot
        )
        return
    tree, manifest = fixture()
    await on_lane_system(
        run,
        base_url,
        issuer,
        db_url,
        project=f"cov-{secrets.token_hex(4)}",
        body=partial(functional, run, caller, tree, manifest, db_url),
    )


def _cells() -> list[Cell]:
    return [
        c
        for c in tool_cells(TOOLS)
        if (c.provider == "local-libvirt" and c.guest_arch == platform.machine())
        or (c.provider == "remote-libvirt" and c.guest_arch in REMOTE_LANE_FAMILIES)
    ]


@pytest.mark.parametrize("cell", _cells(), ids=lambda cell: cell.id)
def test_control_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a traffic, SysRq or watch tool."""
    run_tool_cell(cell, _scenario)
