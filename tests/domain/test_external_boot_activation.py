"""Domain proofs for durable external-boot activation persistence (ADR-0583)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import product
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from kdive.domain.capacity.state import can_transition
from kdive.domain.external_boot_activation import (
    ExternalBootActivation,
    ExternalBootActivationState,
    ExternalBootCleanupEvidenceV1,
    ExternalBootConflictEvidenceV1,
    ExternalBootPreRecoveryEvidenceV1,
    ExternalBootRecoveryAttempt,
    ExternalBootRecoveryAttemptState,
    ExternalBootReleaseEvidenceV1,
    ExternalBootReleaseObject,
    ExternalBootReservation,
    ExternalBootReservationRelease,
    ExternalBootReservationState,
    ExternalBootTeardownEvidenceV1,
    ExternalBootTerminalEvidenceV1,
)
from kdive.providers.ports.external_boot import OpaqueProviderRef

_ACTIVATION_ID = UUID("11111111-1111-1111-1111-111111111111")
_SYSTEM_ID = UUID("22222222-2222-2222-2222-222222222222")
_RUN_ID = UUID("33333333-3333-3333-3333-333333333333")
_OBSERVATION_ID = UUID("44444444-4444-4444-4444-444444444444")
_AT = datetime(2026, 8, 28, tzinfo=UTC)
_PLAN = "sha256:" + "a" * 64
_STATE = "sha256:" + "b" * 64
_RELEASE = "sha256:" + "c" * 64
_TEARDOWN = "sha256:" + "d" * 64

_ACTIVATION_EDGES = {
    ("preparing", "prepared"),
    ("preparing", "abandoned"),
    ("preparing", "recovery_conflict"),
    ("prepared", "activating"),
    ("prepared", "recovering"),
    ("prepared", "recovery_conflict"),
    ("activating", "active"),
    ("activating", "recovering"),
    ("activating", "recovery_conflict"),
    ("active", "recovering"),
    ("active", "recovery_conflict"),
    ("recovering", "recovered"),
    ("recovering", "recovery_failed"),
    ("recovering", "recovery_conflict"),
    ("recovery_conflict", "recovering"),
}


@pytest.mark.parametrize(
    ("source", "target"), tuple(product(ExternalBootActivationState, repeat=2))
)
def test_activation_transition_table_is_exhaustive(
    source: ExternalBootActivationState, target: ExternalBootActivationState
) -> None:
    assert can_transition(source, target) is ((source.value, target.value) in _ACTIVATION_EDGES)


@given(
    st.sampled_from(tuple(ExternalBootActivationState)),
    st.sampled_from(tuple(ExternalBootActivationState)),
)
def test_activation_transition_property(
    source: ExternalBootActivationState, target: ExternalBootActivationState
) -> None:
    assert can_transition(source, target) is ((source.value, target.value) in _ACTIVATION_EDGES)


@pytest.mark.parametrize(
    ("source", "target"), tuple(product(ExternalBootReservationState, repeat=2))
)
def test_reservation_transition_table_is_exhaustive(
    source: ExternalBootReservationState, target: ExternalBootReservationState
) -> None:
    assert can_transition(source, target) is (
        source is ExternalBootReservationState.PENDING
        and target is ExternalBootReservationState.READY
    )


def _pre_recovery() -> ExternalBootPreRecoveryEvidenceV1:
    return ExternalBootPreRecoveryEvidenceV1(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        run_id=_RUN_ID,
        plan_identity=_PLAN,
        recovery_object=OpaqueProviderRef(ref="recovery/object-1"),
        source_composite_state=_STATE,
        observed_at=_AT,
    )


def test_pre_recovery_fixed_vector() -> None:
    evidence = _pre_recovery()
    assert evidence.to_canonical_json() == (
        b'{"activation_id":"11111111-1111-1111-1111-111111111111",'
        b'"observed_at":"2026-08-28T00:00:00Z",'
        b'"plan_identity":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"recovery_object":{"ref":"recovery/object-1"},'
        b'"run_id":"33333333-3333-3333-3333-333333333333",'
        b'"schema":"external-boot-pre-recovery-evidence-v1",'
        b'"source_composite_state":"sha256:'
        b'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"system_id":"22222222-2222-2222-2222-222222222222"}'
    )
    assert evidence.identity == (
        "sha256:76aa7c43e0423a3dbf594c556dccbac8b98aed727d7e1978b47a96486015ad35"
    )
    assert (
        ExternalBootPreRecoveryEvidenceV1.from_canonical_json(evidence.to_canonical_json())
        == evidence
    )


def test_evidence_bytes_must_be_canonical_and_bounded() -> None:
    canonical = _pre_recovery().to_canonical_json()
    with pytest.raises(ValueError, match="not canonical"):
        ExternalBootPreRecoveryEvidenceV1.from_canonical_json(b" " + canonical)
    with pytest.raises(ValueError, match="65536"):
        ExternalBootPreRecoveryEvidenceV1.from_canonical_json(b" " * 65_537)


def test_evidence_requires_utc_and_forbids_extra_fields() -> None:
    values = _pre_recovery().model_dump(by_alias=True)
    values["observed_at"] = _AT.astimezone(UTC) + timedelta(hours=1)
    values["observed_at"] = values["observed_at"].replace(tzinfo=None)
    with pytest.raises(ValidationError, match="UTC"):
        ExternalBootPreRecoveryEvidenceV1.model_validate(values)
    values = _pre_recovery().model_dump(by_alias=True)
    values["unknown_field"] = "do-not-store"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ExternalBootPreRecoveryEvidenceV1.model_validate(values)


def test_all_evidence_versions_are_closed_and_domain_separated() -> None:
    objects = (OpaqueProviderRef(ref="objects/a"),)
    conflict = ExternalBootConflictEvidenceV1(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        observation_id=_OBSERVATION_ID,
        composite_state=_STATE,
        objects=objects,
        observed_at=_AT,
    )
    terminal = ExternalBootTerminalEvidenceV1(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        outcome="active",
        composite_state=_STATE,
        objects=objects,
        observed_at=_AT,
    )
    release = ExternalBootReleaseEvidenceV1(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        store_identity=OpaqueProviderRef(ref="stores/main"),
        owner_key=OpaqueProviderRef(ref="owners/activation-1"),
        reserved_bytes=1,
        objects=(ExternalBootReleaseObject(object=OpaqueProviderRef(ref="objects/a")),),
        verified_at=_AT,
    )
    teardown = ExternalBootTeardownEvidenceV1(system_id=_SYSTEM_ID, observed_at=_AT)
    cleanup = ExternalBootCleanupEvidenceV1(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        release_identity=_RELEASE,
        mode="system_teardown",
        teardown_identity=_TEARDOWN,
        completed_at=_AT,
    )
    evidences = (conflict, _pre_recovery(), terminal, release, teardown, cleanup)
    assert len({value.identity for value in evidences}) == len(evidences)
    assert all(value.to_canonical_json() for value in evidences)


def test_object_sets_must_be_sorted_and_duplicate_free() -> None:
    common = dict(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        observation_id=_OBSERVATION_ID,
        composite_state=_STATE,
        observed_at=_AT,
    )
    with pytest.raises(ValidationError, match="sorted"):
        ExternalBootConflictEvidenceV1.model_validate(
            common
            | {
                "objects": (
                    OpaqueProviderRef(ref="objects/z"),
                    OpaqueProviderRef(ref="objects/a"),
                )
            }
        )
    with pytest.raises(ValidationError, match="duplicate"):
        ExternalBootConflictEvidenceV1.model_validate(
            common
            | {
                "objects": (
                    OpaqueProviderRef(ref="objects/a"),
                    OpaqueProviderRef(ref="objects/a"),
                )
            }
        )


def test_cleanup_mode_controls_teardown_identity() -> None:
    common = dict(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        release_identity=_RELEASE,
        completed_at=_AT,
    )
    with pytest.raises(ValidationError, match="teardown_identity"):
        ExternalBootCleanupEvidenceV1.model_validate(
            common | {"mode": "ordinary", "teardown_identity": _TEARDOWN}
        )
    with pytest.raises(ValidationError, match="teardown_identity"):
        ExternalBootCleanupEvidenceV1.model_validate(common | {"mode": "system_teardown"})


def test_activation_row_enforces_cleanup_matrix_and_positive_generation() -> None:
    common = dict(
        id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        run_id=_RUN_ID,
        plan_identity=_PLAN,
        operation_owner_id=_OBSERVATION_ID,
        authority_generation=1,
        state=ExternalBootActivationState.PREPARING,
        cleanup_complete=False,
        created_at=_AT,
        updated_at=_AT,
    )
    activation = ExternalBootActivation.model_validate(common)
    assert activation.authority_generation == 1
    with pytest.raises(ValidationError, match="cleanup_complete"):
        ExternalBootActivation.model_validate(common | {"cleanup_complete": True})
    with pytest.raises(ValidationError, match="greater than 0"):
        ExternalBootActivation.model_validate(common | {"authority_generation": 0})
    with pytest.raises(ValidationError, match="materialization"):
        ExternalBootActivation.model_validate(
            common | {"state": "active", "activation_readiness_deadline": _AT}
        )


def test_release_row_binds_identity_and_evidence_fields() -> None:
    evidence = ExternalBootReleaseEvidenceV1(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        store_identity=OpaqueProviderRef(ref="stores/main"),
        owner_key=OpaqueProviderRef(ref="owners/activation-1"),
        reserved_bytes=4096,
        objects=(),
        verified_at=_AT,
    )
    common = dict(
        activation_id=_ACTIVATION_ID,
        store_identity="stores/main",
        owner_key="owners/activation-1",
        reserved_bytes=4096,
        release_identity=evidence.identity,
        release_evidence=evidence,
        released_at=_AT,
    )
    assert ExternalBootReservationRelease.model_validate(common).release_evidence == evidence
    with pytest.raises(ValidationError, match="release identity"):
        ExternalBootReservationRelease.model_validate(common | {"release_identity": _RELEASE})
    with pytest.raises(ValidationError, match="release evidence ownership"):
        ExternalBootReservationRelease.model_validate(common | {"store_identity": "stores/other"})


def test_reservation_row_requires_ready_timestamp_exactly_when_ready() -> None:
    common = dict(
        activation_id=_ACTIVATION_ID,
        store_identity="stores/main",
        owner_key="owners/activation-1",
        reserved_bytes=4096,
        created_at=_AT,
        updated_at=_AT,
    )
    assert ExternalBootReservation.model_validate(common | {"state": "pending"}).ready_at is None
    assert (
        ExternalBootReservation.model_validate(
            common | {"state": "ready", "ready_at": _AT}
        ).ready_at
        == _AT
    )
    with pytest.raises(ValidationError, match="ready_at"):
        ExternalBootReservation.model_validate(common | {"state": "ready"})


def test_recovery_attempt_row_enforces_deadline_and_evidence_state() -> None:
    conflict = ExternalBootConflictEvidenceV1(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        observation_id=_OBSERVATION_ID,
        composite_state=_STATE,
        objects=(),
        observed_at=_AT,
    )
    common = dict(
        activation_id=_ACTIVATION_ID,
        attempt_number=1,
        attempt_id=_OBSERVATION_ID,
        authority_generation=1,
        recovery_basis="pre_recovery",
        created_at=_AT,
        updated_at=_AT,
    )
    attempt = ExternalBootRecoveryAttempt.model_validate(
        common
        | {
            "state": ExternalBootRecoveryAttemptState.CONFLICT,
            "conflict_evidence": conflict,
        }
    )
    assert attempt.recovery_readiness_deadline is None
    with pytest.raises(ValidationError, match="recovery_readiness_deadline"):
        ExternalBootRecoveryAttempt.model_validate(
            common | {"state": ExternalBootRecoveryAttemptState.RECOVERING}
        )
    wrong_conflict = conflict.model_copy(update={"activation_id": _RUN_ID})
    with pytest.raises(ValidationError, match="conflict evidence ownership"):
        ExternalBootRecoveryAttempt.model_validate(
            common
            | {
                "state": ExternalBootRecoveryAttemptState.CONFLICT,
                "conflict_evidence": wrong_conflict,
            }
        )
    wrong_terminal = ExternalBootTerminalEvidenceV1(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        outcome="active",
        composite_state=_STATE,
        objects=(),
        observed_at=_AT,
    )
    with pytest.raises(ValidationError, match="terminal evidence"):
        ExternalBootRecoveryAttempt.model_validate(
            common
            | {
                "state": ExternalBootRecoveryAttemptState.RECOVERED,
                "terminal_evidence": wrong_terminal,
            }
        )
