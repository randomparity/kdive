"""RESTORING/PAUSED are counted live/non-terminal at every state-keyed site (#1254, ADR-0378).

A RESTORING or PAUSED System holds a live host domain and a quota slot, and its console keeps
streaming/sealing — so both states must be present in every membership set that enumerates the
"live" System states. (The whole-tree discovery sweep in test_state_site_coverage.py is the
backstop; these assertions pin the specific sets this change touched.)
"""

from __future__ import annotations

import pytest

from kdive.domain.capacity.state import SystemState
from kdive.jobs.handlers.console.console_rotate import _LIVE_STATES
from kdive.providers.infra.console_hosting import _RUNNING_SYSTEM_STATE_VALUES
from kdive.reconciler.repairs.allocations import _LIVE_SYSTEM_STATES
from kdive.services.systems.admission import _NON_TERMINAL_SYSTEM


@pytest.mark.parametrize("state", [SystemState.RESTORING, SystemState.PAUSED])
def test_state_is_non_terminal_and_live(state: SystemState) -> None:
    assert state in _NON_TERMINAL_SYSTEM  # holds a quota slot
    assert state in _LIVE_SYSTEM_STATES  # not orphaning its allocation
    assert state in _LIVE_STATES  # console sealing keeps running
    assert state.value in _RUNNING_SYSTEM_STATE_VALUES  # remote console keeps streaming
