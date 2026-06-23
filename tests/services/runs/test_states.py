"""Pin the run-lifecycle state sets shared by services and MCP renderers.

These are derived from the state enums; a mutant that drops, adds, or swaps a member must fail.
"""

from __future__ import annotations

from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.services.runs import states


def test_run_hostable_is_ready_only() -> None:
    assert frozenset({SystemState.READY}) == states.RUN_HOSTABLE


def test_system_gone_is_the_three_dead_states() -> None:
    assert (
        frozenset({SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED})
        == states.SYSTEM_GONE
    )


def test_alloc_hostable_is_active_only() -> None:
    assert frozenset({AllocationState.ACTIVE}) == states.ALLOC_HOSTABLE


def test_investigation_open_for_run_is_open_and_active() -> None:
    assert (
        frozenset({InvestigationState.OPEN, InvestigationState.ACTIVE})
        == states.INVESTIGATION_OPEN_FOR_RUN
    )


def test_run_build_terminal_is_failed_and_canceled() -> None:
    assert frozenset({RunState.FAILED, RunState.CANCELED}) == states.RUN_BUILD_TERMINAL


def test_run_non_terminal_is_created_and_running() -> None:
    assert frozenset({RunState.CREATED, RunState.RUNNING}) == states.RUN_NON_TERMINAL


def test_run_bindable_is_created_running_succeeded() -> None:
    assert (
        frozenset({RunState.CREATED, RunState.RUNNING, RunState.SUCCEEDED}) == states.RUN_BINDABLE
    )


def test_bindable_excludes_terminal_build_states() -> None:
    # a Run that failed or was canceled is never bindable
    assert states.RUN_BUILD_TERMINAL.isdisjoint(states.RUN_BINDABLE)
