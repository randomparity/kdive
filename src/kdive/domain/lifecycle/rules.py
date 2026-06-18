"""Shared lifecycle state sets used across MCP and worker code."""

from __future__ import annotations

from kdive.domain.capacity.state import AllocationState, SystemState

# Allocation states that still hold live capacity and PCIe claims. Derived from the enum,
# not literal strings, so it cannot drift if the state machine gains a value.
NON_TERMINAL_ALLOCATION_STATES = (
    AllocationState.REQUESTED,
    AllocationState.GRANTED,
    AllocationState.ACTIVE,
    AllocationState.RELEASING,
)
NON_TERMINAL_ALLOCATION_STATE_VALUES = tuple(
    state.value for state in NON_TERMINAL_ALLOCATION_STATES
)

TERMINAL_SYSTEM_STATES = frozenset({SystemState.TORN_DOWN, SystemState.FAILED})
