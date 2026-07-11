"""CRASHING must join the live/non-terminal state sets, not the terminal/hostable ones (#1078)."""

from __future__ import annotations

from kdive.domain.capacity.state import SystemState
from kdive.domain.lifecycle.rules import TERMINAL_SYSTEM_STATES
from kdive.jobs.handlers.console_rotate import _LIVE_STATES
from kdive.providers.infra.console_hosting import _RUNNING_SYSTEM_STATE_VALUES
from kdive.reconciler.repairs.allocations import _LIVE_SYSTEM_STATES as _ALLOC_LIVE
from kdive.reconciler.repairs.console_rotation import _LIVE_SYSTEM_STATES as _ROT_LIVE
from kdive.services.runs.states import RUN_HOSTABLE, SYSTEM_GONE
from kdive.services.systems.admission import _NON_TERMINAL_SYSTEM


def test_crashing_is_live_and_non_terminal() -> None:
    assert SystemState.CRASHING in _NON_TERMINAL_SYSTEM  # occupies a quota slot
    assert SystemState.CRASHING in _ALLOC_LIVE  # allocation not orphaned mid-crash
    assert SystemState.CRASHING.value in _RUNNING_SYSTEM_STATE_VALUES  # console keeps streaming
    assert SystemState.CRASHING in _LIVE_STATES  # console rotation live
    assert SystemState.CRASHING.value in _ROT_LIVE  # reconciler console rotation live


def test_crashing_is_not_hostable_gone_or_terminal() -> None:
    assert SystemState.CRASHING not in RUN_HOSTABLE  # no new Run on a crashing System
    assert SystemState.CRASHING not in SYSTEM_GONE  # transient, not gone
    assert SystemState.CRASHING not in TERMINAL_SYSTEM_STATES
