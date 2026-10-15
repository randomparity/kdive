"""Prove the x86_64 local GDB debug tool cells over HTTP (#2813).

``live_stack``-marked (ADR-0722). One parameter per native local-libvirt contract cell of the 21
``debug.*`` tools, framed by :func:`~tests.integration.live_stack.tool_cells.run_tool_cell`; the
scenarios are in :mod:`tests.integration.live_stack.debug_cells`. The remote cells are #3113's and
the ppc64le cells #2818's. ``docs/operating/runbooks/live-testing.md`` covers the run.
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
from tests.integration.live_stack.debug_cells import (
    TOOLS,
    debug_target,
    functional,
    provision_gdbstub,
    rejection,
    snapshot_of,
)
from tests.integration.live_stack.deep_lifecycle import FIXTURE_ROOT_ENV, load_fixture
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import (
    Exposure,
    HttpCaller,
    boundary_of,
    on_lane_system,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

# The kernel fixture every functional cell and the rejection target boot; write the bindings
# with ``--kernel-baseline longterm``.
BASELINE = "longterm"


def _fixture() -> tuple[Path, dict[str, Any]]:
    """The verified ``longterm`` fixture; ``blocked`` before any stack mutation when absent."""
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the {BASELINE} fixture"
        )
    try:
        return load_fixture(Path(root), BASELINE, platform.machine())
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{BASELINE} fixture: {exc}") from None


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    cell = run.cell
    if cell.guest_arch != "x86_64":
        raise ScenarioStop(Outcome.BLOCKED, f"{cell.id} is the POWER lane's (#2818)")
    caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
    variant = cell.scenario_id.split("/")[2]
    if cell.kind == "rejection":
        boundary = boundary_of(cell)
        target = await debug_target(run, base_url, issuer, db_url, _fixture)
        await prove_rejection(
            run,
            caller,
            boundary,
            rejection(cell.operation, boundary, target, variant),
            snapshot_of(db_url, target),
        )
        return
    tree, manifest = _fixture()
    await on_lane_system(
        run,
        base_url,
        issuer,
        db_url,
        project=f"cov-{secrets.token_hex(4)}",
        body=partial(functional, run, caller, tree, manifest, db_url),
        provision=provision_gdbstub,
    )


def _cells() -> list[Cell]:
    return [
        c
        for c in tool_cells(TOOLS)
        if c.provider == "local-libvirt" and c.guest_arch == platform.machine()
    ]


@pytest.mark.parametrize("cell", _cells(), ids=lambda cell: cell.id)
def test_debug_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a local GDB debug tool."""
    run_tool_cell(cell, _scenario)
