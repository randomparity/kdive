"""Pin the shared lifecycle state sets used across MCP and worker code."""

from __future__ import annotations

from kdive.domain.capacity.state import AllocationState, SystemState
from kdive.domain.lifecycle import rules


def test_non_terminal_allocation_states_are_the_four_live_states_in_order() -> None:
    assert rules.NON_TERMINAL_ALLOCATION_STATES == (
        AllocationState.REQUESTED,
        AllocationState.GRANTED,
        AllocationState.ACTIVE,
        AllocationState.RELEASING,
    )


def test_non_terminal_allocation_state_values_mirror_the_enum_values() -> None:
    assert (
        tuple(state.value for state in rules.NON_TERMINAL_ALLOCATION_STATES)
        == rules.NON_TERMINAL_ALLOCATION_STATE_VALUES
    )


def test_terminal_system_states_are_torn_down_and_failed() -> None:
    assert frozenset({SystemState.TORN_DOWN, SystemState.FAILED}) == rules.TERMINAL_SYSTEM_STATES


def test_non_terminal_allocation_states_exclude_released() -> None:
    assert AllocationState.RELEASED not in rules.NON_TERMINAL_ALLOCATION_STATES
