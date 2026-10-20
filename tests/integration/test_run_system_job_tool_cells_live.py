"""Real Run/System/Job service cells (#3097); synthetic builds prove validation only."""

from __future__ import annotations

import pytest

from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.run_system_job_cells import (
    TOOLS,
    _scenario,
)
from tests.integration.live_stack.tool_cells import (
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack


@pytest.mark.parametrize("cell", tool_cells(TOOLS), ids=lambda cell: cell.id)
def test_run_system_job_tool_cell(cell: Cell) -> None:
    run_tool_cell(cell, _scenario)
