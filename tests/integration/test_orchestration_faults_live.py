"""Orchestration-fault cells: worker interruption, lost response, backing service (#3117).

``live_stack``-marked. One parameter per ``failure-resource/<provider>/x86_64`` cell #3117
owns; each writes one version-1 ``Evidence`` record under ``KDIVE_ARTIFACT_DIR``.
``docs/operating/runbooks/live-testing.md`` covers the preconditions, bindings and run.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.orchestration_faults import (
    OWNER,
    backing_service,
    lost_response,
    worker_interruption,
)
from tests.integration.live_stack.resource_limits import cells, failure
from tests.integration.live_stack.scenario import run_cell

pytestmark = pytest.mark.live_stack

_SCENARIOS = {
    "worker-interruption": worker_interruption,
    "lost-response": lost_response,
    "backing-service": backing_service,
}


@pytest.mark.parametrize("cell", cells("local-libvirt", owner=OWNER), ids=failure)
def test_local_orchestration_fault(cell: Cell, tmp_path: Path) -> None:
    """Prove the cell's recovery outcome, no duplicates, protected state and cleanup."""
    run_cell(cell, partial(_SCENARIOS[failure(cell)], tmp=tmp_path, remote=False))


@pytest.mark.parametrize("cell", cells("remote-libvirt", owner=OWNER), ids=failure)
def test_remote_orchestration_fault(cell: Cell, tmp_path: Path) -> None:
    """The same proofs on a separate remote-libvirt provider host (``REMOTE_PROVIDER_SSH``)."""
    run_cell(cell, partial(_SCENARIOS[failure(cell)], tmp=tmp_path, remote=True))
