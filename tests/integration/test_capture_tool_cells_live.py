"""Prove the x86_64 crash capture and introspection tool cells over HTTP on both providers.

``live_stack``-marked (ADR-0722). One parameter per native local-libvirt contract cell (#2814)
and per x86_64 remote-libvirt contract cell (#3114) of ``control.force_crash``, ``vmcore.fetch``,
``introspect.from_vmcore``, ``introspect.run``, ``introspect.script`` and ``postmortem.crash``,
framed by :func:`~tests.integration.live_stack.tool_cells.run_tool_cell`; the scenarios are in
:mod:`tests.integration.live_stack.capture_cells`. A remote cell runs on the provider host of
``docs/operating/runbooks/remote-live-stack.md``; its handler-refused ``vmcore.fetch`` variants
prove the refusal (ADR-0722, 2026-10-20). The ppc64le cells are #2818's.
``docs/operating/runbooks/live-testing.md`` covers the local run.
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
from scripts.coverage_campaign.contract import HANDLER_REFUSAL, Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.capture_cells import (
    TOOLS,
    capability,
    capture_target,
    functional,
    options,
    provision_with,
    refused_method,
    rejection,
    snapshot_of,
    unsupported,
    variant_of,
)
from tests.integration.live_stack.deep_lifecycle import FIXTURE_ROOT_ENV, load_fixture
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import (
    REMOTE_LANE_FAMILIES,
    Exposure,
    HttpCaller,
    LaneBody,
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
    fixture = partial(_fixture, str(cell.guest_arch))
    caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
    if cell.kind == "rejection":
        boundary = boundary_of(cell)
        if boundary == "capability":
            assert cell.provider == "local-libvirt", f"{cell.id}: no remote capability cell"
            await capability(run, caller, base_url, issuer, db_url)
            return
        target = await capture_target(run, base_url, issuer, db_url, fixture)
        call = rejection(cell.operation, boundary, target, method=variant_of(cell))
        await prove_rejection(run, caller, boundary, call, snapshot_of(db_url, target))
        return
    crash = options(cell)
    tree, manifest = fixture()
    project = f"cov-{secrets.token_hex(4)}"
    body: LaneBody
    if cell.unsupported_reason == HANDLER_REFUSAL:
        body = partial(refused_method, run, caller, tree, manifest, db_url)
    elif cell.kind == "unsupported":
        body = partial(unsupported, run, caller, tree, manifest)
    else:
        body = partial(functional, run, caller, tree, manifest)
    await on_lane_system(
        run,
        base_url,
        issuer,
        db_url,
        project=project,
        body=body,
        provision=provision_with(crash),
    )


def _cells() -> list[Cell]:
    host = platform.machine()
    return [
        c
        for c in tool_cells(TOOLS)
        if (c.provider == "local-libvirt" and c.guest_arch == host)
        or (c.provider == "remote-libvirt" and c.guest_arch in REMOTE_LANE_FAMILIES)
    ]


@pytest.mark.parametrize("cell", _cells(), ids=lambda cell: cell.id)
def test_capture_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a capture or introspection tool."""
    run_tool_cell(cell, _scenario)
