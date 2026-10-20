"""Prove the x86_64 power and snapshot tool cells over HTTP on both providers (#2815, #3116).

``live_stack``-marked (ADR-0722). One parameter per native local-libvirt contract cell and per
x86_64 remote-libvirt contract cell of ``control.power``, ``systems.snapshot``,
``systems.restore``, ``systems.list_snapshots`` and ``systems.delete_snapshot``, framed by
:func:`~tests.integration.live_stack.tool_cells.run_tool_cell`; the scenarios are in
:mod:`tests.integration.live_stack.power_snapshots`, observed on the cell's provider. The ppc64le
cells are #2818's. ``docs/operating/runbooks/live-testing.md`` covers the local run,
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
from tests.integration.live_stack.deep_lifecycle import FIXTURE_ROOT_ENV, load_fixture
from tests.integration.live_stack.power_snapshots import (
    TOOLS,
    functional,
    grants,
    rejection,
    snapshot_of,
)
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import (
    REMOTE_LANE_FAMILIES,
    Exposure,
    HttpCaller,
    boundary_of,
    lane_for,
    lane_target,
    on_lane_system,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

# The kernel fixture every functional cell boots; write the bindings with
# ``--kernel-baseline longterm``.
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
    caller = HttpCaller(cast(Exposure, run.cell.exposure), base_url, issuer)
    if run.cell.kind == "functional":
        tree, manifest = _fixture(str(run.cell.guest_arch))
        project = f"cov-{secrets.token_hex(4)}"
        token = caller.token(grants(project, "contributor"))
        await on_lane_system(
            run,
            base_url,
            issuer,
            db_url,
            project=project,
            body=partial(functional, run, caller, token, tree, manifest),
        )
        return
    target = await lane_target(run, base_url, issuer, db_url)
    boundary = boundary_of(run.cell)
    await prove_rejection(
        run,
        caller,
        boundary,
        rejection(run.cell.operation, boundary, target),
        snapshot_of(db_url, target, lane_for(run.cell).connect),
    )


def _cells() -> list[Cell]:
    return [
        c
        for c in tool_cells(TOOLS)
        if (c.provider == "local-libvirt" and c.guest_arch == platform.machine())
        or (c.provider == "remote-libvirt" and c.guest_arch in REMOTE_LANE_FAMILIES)
    ]


@pytest.mark.parametrize("cell", _cells(), ids=lambda cell: cell.id)
def test_power_snapshot_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a power or snapshot tool."""
    run_tool_cell(cell, _scenario)
