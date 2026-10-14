"""Resource-boundary cells: minimum sizing, insufficient disk, slow boot (#3118).

``live_stack``-marked. One parameter per ``failure-resource/local-libvirt/x86_64`` cell #3118
owns; each writes one version-1 ``Evidence`` record under ``KDIVE_ARTIFACT_DIR``.
``docs/operating/runbooks/live-testing.md`` covers the fixtures, bindings and run.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.resource_limits import (
    cells,
    failure,
    local_insufficient_disk,
    minimum,
    slow_boot,
)
from tests.integration.live_stack.scenario import run_cell

pytestmark = pytest.mark.live_stack

_SCENARIOS = {
    "minimum-cpu-ram": partial(minimum, remote=False),
    "insufficient-disk": local_insufficient_disk,
    "slow-boot": partial(slow_boot, remote=False),
}


@pytest.mark.parametrize("cell", cells("local-libvirt"), ids=failure)
def test_local_resource_boundary(cell: Cell, tmp_path: Path) -> None:
    """Prove the cell's resource outcome, no duplicates, protected state and cleanup."""
    run_cell(cell, partial(_SCENARIOS[failure(cell)], tmp=tmp_path))


_REMOTE = {
    "minimum-cpu-ram": partial(minimum, remote=True),
    "slow-boot": partial(slow_boot, remote=True),
}


@pytest.mark.parametrize(
    "cell", [c for c in cells("remote-libvirt") if failure(c) in _REMOTE], ids=failure
)
def test_remote_resource_boundary(cell: Cell, tmp_path: Path) -> None:
    """The same proofs on a separate remote-libvirt provider host (``REMOTE_PROVIDER_SSH``)."""
    run_cell(cell, partial(_REMOTE[failure(cell)], tmp=tmp_path))
