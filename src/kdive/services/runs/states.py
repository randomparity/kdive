"""Run lifecycle state sets shared by services and MCP renderers."""

from __future__ import annotations

from kdive.domain.state import AllocationState, InvestigationState, RunState, SystemState

RUN_HOSTABLE = frozenset({SystemState.READY})
SYSTEM_GONE = frozenset({SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED})
ALLOC_HOSTABLE = frozenset({AllocationState.ACTIVE})
INVESTIGATION_OPEN_FOR_RUN = frozenset({InvestigationState.OPEN, InvestigationState.ACTIVE})
RUN_BUILD_TERMINAL = frozenset({RunState.FAILED, RunState.CANCELED})
RUN_NON_TERMINAL = frozenset({RunState.CREATED, RunState.RUNNING})
