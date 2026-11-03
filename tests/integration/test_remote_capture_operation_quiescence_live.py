"""Remote real-provider capture-operation ordering carrier (ADR-0558)."""

from __future__ import annotations

import pytest

from tests.integration.live_stack.remote_quiescence import cells, scenario
from tests.integration.live_stack.scenario import run_cell


@pytest.mark.live_vm
@pytest.mark.live_vm_remote
def test_remote_capture_operation_waits_for_fresh_monitor_ordering() -> None:
    """Run the Resource-bound TLS proof against an accepted, held native NBD operation."""
    selected = cells()
    assert len(selected) == 1, "expected one native x86 remote quiescence cell"
    run_cell(selected[0], scenario)
