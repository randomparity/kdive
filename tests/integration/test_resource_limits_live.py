"""Local-libvirt resource-boundary cells: minimum sizing, insufficient disk, slow boot (#3118).

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
    local_minimum,
    local_slow_boot,
)
from tests.integration.live_stack.scenario import run_cell

pytestmark = pytest.mark.live_stack

_SCENARIOS = {
    "minimum-cpu-ram": local_minimum,
    "insufficient-disk": local_insufficient_disk,
    "slow-boot": local_slow_boot,
}


@pytest.mark.parametrize("cell", cells("local-libvirt"), ids=failure)
def test_local_resource_boundary(cell: Cell, tmp_path: Path) -> None:
    """Prove the cell's resource outcome, no duplicates, protected state and cleanup."""
    run_cell(cell, partial(_SCENARIOS[failure(cell)], tmp=tmp_path))
