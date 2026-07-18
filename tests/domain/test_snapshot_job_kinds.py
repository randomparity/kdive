"""The snapshot job kinds and the resume power action (#1254, ADR-0378)."""

from __future__ import annotations

from kdive.domain.operations.jobs import (
    ACTIVE_JOB_KINDS,
    CONTRIBUTOR_CANCELABLE_JOB_KINDS,
    OPT_IN_DESTRUCTIVE_JOB_KINDS,
    JobKind,
    PowerAction,
)

_SNAPSHOT_KINDS = (JobKind.SNAPSHOT, JobKind.RESTORE, JobKind.DELETE_SNAPSHOT)


def test_snapshot_kinds_are_active_and_contributor_cancelable() -> None:
    for kind in _SNAPSHOT_KINDS:
        # Enqueuable/filterable, and a contributor can cancel its own snapshot/restore/delete
        # (the cancel gate fails closed to operator-only for any kind absent from the set).
        assert kind in ACTIVE_JOB_KINDS
        assert kind in CONTRIBUTOR_CANCELABLE_JOB_KINDS


def test_snapshot_kinds_are_not_destructive_opt_in() -> None:
    # Restore is destructive to a running Run but gates via `contributor` + a fencing state,
    # not the force_crash opt-in gate (which stays reserved for force_crash).
    for kind in _SNAPSHOT_KINDS:
        assert kind not in OPT_IN_DESTRUCTIVE_JOB_KINDS


def test_power_action_resume_exists() -> None:
    assert PowerAction.RESUME.value == "resume"
    assert PowerAction.RESUME not in {
        PowerAction.ON,
        PowerAction.OFF,
        PowerAction.CYCLE,
        PowerAction.RESET,
    }
