"""The state-fenced kind set and its dispatch-lane derivation (#1538, ADR-0550)."""

from __future__ import annotations

from kdive.domain.operations.jobs import (
    ACTIVE_JOB_KINDS,
    DEFAULT_JOB_DISPATCH_LANE,
    STATE_FENCED_JOB_DISPATCH_LANE,
    STATE_FENCED_JOB_KINDS,
    JobKind,
    dispatch_lane_for_kind,
)


def test_the_set_is_exactly_the_three_kinds_that_fence_at_enqueue() -> None:
    # The rule is "the enqueue transaction writes a transient state another tool rejects on":
    # restore -> SystemState.RESTORING, reprovision -> SystemState.REPROVISIONING,
    # snapshot -> SnapshotState.CREATING. Pinned as an equality, not a membership check, so
    # adding a kind without revisiting the rule fails here.
    assert (
        frozenset({JobKind.RESTORE, JobKind.REPROVISION, JobKind.SNAPSHOT})
        == STATE_FENCED_JOB_KINDS
    )


def test_the_near_miss_kinds_stay_on_the_default_lane() -> None:
    # delete_snapshot writes no state (only its queued presence is read, by
    # _active_snapshot_op); teardown's handler writes the state, not its enqueue; provision has
    # no pre-existing object to fence. ADR-0550 rejects all three.
    for kind in (JobKind.DELETE_SNAPSHOT, JobKind.TEARDOWN, JobKind.PROVISION):
        assert kind not in STATE_FENCED_JOB_KINDS
        assert dispatch_lane_for_kind(kind) == DEFAULT_JOB_DISPATCH_LANE


def test_a_retired_kind_can_never_be_routed() -> None:
    assert STATE_FENCED_JOB_KINDS <= ACTIVE_JOB_KINDS


def test_the_derivation_is_total_over_every_job_kind() -> None:
    # Spec S2's derivation half: every kind derives a lane, and only the fenced three derive the
    # fenced lane. No payload needed, so this covers kinds the enqueue-level test cannot reach.
    for kind in JobKind:
        expected = (
            STATE_FENCED_JOB_DISPATCH_LANE
            if kind in STATE_FENCED_JOB_KINDS
            else DEFAULT_JOB_DISPATCH_LANE
        )
        assert dispatch_lane_for_kind(kind) == expected


def test_the_two_lane_names_are_distinct_and_non_empty() -> None:
    # jobs_dispatch_lane_nonempty (migration 0066) rejects a blank lane at the database.
    assert STATE_FENCED_JOB_DISPATCH_LANE != DEFAULT_JOB_DISPATCH_LANE
    assert STATE_FENCED_JOB_DISPATCH_LANE
