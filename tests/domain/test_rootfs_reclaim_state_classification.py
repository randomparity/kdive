"""Structural drift guard for the investigation-rootfs reclaim state classification (ADR-0441 §6).

The reclaim sweep's condition (b) trusts one named allowlist of pre-overlay/re-materialize states
(:data:`ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`). This test reddens if a new non-terminal
``SystemState`` is added without being classified as base-re-materializing (in the pre-overlay set)
or overlay-backed (out) — so a future state that reads/re-creates the base with its overlay
momentarily absent cannot silently escape the gate and have its base unlinked under a live guest.
"""

from __future__ import annotations

from kdive.domain.capacity.state import (
    _TRANSITIONS,
    ROOTFS_BASE_OVERLAY_BACKED_SYSTEM_STATES,
    ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES,
    SystemState,
)


def _terminal_system_states() -> frozenset[SystemState]:
    """SystemStates with no legal successor, derived from the guard table (never hand-listed)."""
    table = _TRANSITIONS[SystemState]
    return frozenset(state for state in SystemState if not table.get(state))


def test_reclaim_classification_is_exhaustive() -> None:
    terminal = _terminal_system_states()
    classified = (
        ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES | ROOTFS_BASE_OVERLAY_BACKED_SYSTEM_STATES | terminal
    )
    # Every SystemState is classified exactly once: a new non-terminal state added without being
    # placed in the pre-overlay or overlay-backed set leaves the union short and reddens here.
    assert classified == set(SystemState)


def test_reclaim_sets_are_disjoint_and_non_terminal() -> None:
    terminal = _terminal_system_states()
    assert not (ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES & ROOTFS_BASE_OVERLAY_BACKED_SYSTEM_STATES)
    assert not (ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES & terminal)
    assert not (ROOTFS_BASE_OVERLAY_BACKED_SYSTEM_STATES & terminal)


def test_pre_overlay_set_is_the_documented_states() -> None:
    expected = {
        SystemState.PROVISIONING,
        SystemState.REPROVISIONING,
        SystemState.RESTORING,
    }
    assert expected == ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES
