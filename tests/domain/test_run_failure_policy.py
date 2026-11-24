"""Job failure compensation respects Run state edges and install/boot semantics."""

from __future__ import annotations

import pytest

from kdive.domain.capacity.state import RunState, can_transition
from kdive.domain.operations.jobs import JobKind, run_failure_eligible_states


@pytest.mark.parametrize("kind", list(JobKind))
def test_compensation_sources_have_legal_failure_edges(kind: JobKind) -> None:
    states = run_failure_eligible_states(kind)
    assert states
    assert all(can_transition(state, RunState.FAILED) for state in states)
    expected = {RunState.RUNNING}
    if kind is JobKind.INSTALL:
        expected.add(RunState.SUCCEEDED)
    assert states == expected
