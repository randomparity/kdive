"""Closed provider-host authority protocol tests (ADR-0584, #2126)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from kdive.providers.external_boot_authority.protocol import (
    MAX_SIGNED_BIGINT,
    AuthorityAcknowledgementV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
    JournalPhase,
    JournalRecordV1,
    RecoveryObjectBindingV1,
    canonical_record_bytes,
    record_digest,
)

_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64


def _binding() -> dict[str, object]:
    return {
        "authority_id": uuid4(),
        "generation": 1,
        "system_id": uuid4(),
        "activation_id": uuid4(),
        "run_id": uuid4(),
        "plan_identity": _DIGEST,
        "purpose": "recover",
        "provider_kind": "remote-libvirt",
        "authority_instance": "authority-a",
        "operation_identity": "operation-a",
        "operation_digest": _OTHER_DIGEST,
    }


def _takeover(**changes: object) -> AuthorityTakeoverRequestV1:
    values = _binding() | changes
    return AuthorityTakeoverRequestV1.model_validate(values)


def _object(ref: str = "recovery/object-a") -> RecoveryObjectBindingV1:
    return RecoveryObjectBindingV1(system_id=uuid4(), activation_id=uuid4(), reference=ref)


def _mutation(**changes: object) -> AuthorityMutationRequestV1:
    first = _object()
    values = _binding() | {
        "operation": "restore-source",
        "attempt_id": uuid4(),
        "expected_source_identity": _DIGEST,
        "intended_target_identity": _OTHER_DIGEST,
        "recovery_objects": (first,),
    }
    values.update(changes)
    return AuthorityMutationRequestV1.model_validate(values)


@pytest.mark.parametrize(
    "field",
    [
        "authority_id",
        "generation",
        "system_id",
        "activation_id",
        "run_id",
        "plan_identity",
        "purpose",
        "provider_kind",
        "authority_instance",
        "operation_identity",
        "operation_digest",
    ],
)
def test_every_takeover_field_is_retained(field: str) -> None:
    values = _binding()
    assert getattr(AuthorityTakeoverRequestV1.model_validate(values), field) == values[field]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", 0),
        ("generation", MAX_SIGNED_BIGINT + 1),
        ("provider_kind", ""),
        ("authority_instance", "x" * 256),
        ("operation_identity", "é" * 128),
        ("plan_identity", "sha256:" + "A" * 64),
        ("operation_digest", "bad"),
    ],
)
def test_takeover_rejects_invalid_bounded_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _takeover(**{field: value})


def test_values_are_closed() -> None:
    with pytest.raises(ValidationError):
        AuthorityTakeoverRequestV1.model_validate(_binding() | {"provider_definition": "secret"})


def test_mutation_requires_sorted_unique_recovery_objects() -> None:
    first = _object("a")
    second = RecoveryObjectBindingV1(
        system_id=first.system_id, activation_id=first.activation_id, reference="b"
    )
    assert _mutation(recovery_objects=(first, second)).recovery_objects == (first, second)
    with pytest.raises(ValidationError):
        _mutation(recovery_objects=(second, first))
    with pytest.raises(ValidationError):
        _mutation(recovery_objects=(first, first))


def test_mutation_rejects_recovery_object_from_another_binding() -> None:
    binding = _binding()
    foreign = _object()
    with pytest.raises(ValidationError, match="record binding"):
        JournalRecordV1.model_validate(
            binding
            | {
                "sequence": 1,
                "previous_digest": "sha256:" + "0" * 64,
                "phase": JournalPhase.ADMITTED,
                "attempt_id": uuid4(),
                "expected_source_identity": _DIGEST,
                "intended_target_identity": _OTHER_DIGEST,
                "recovery_objects": (foreign,),
            }
        )


def test_mutation_rejects_over_cardinality() -> None:
    owner_system = uuid4()
    owner_activation = uuid4()
    objects = tuple(
        RecoveryObjectBindingV1(
            system_id=owner_system,
            activation_id=owner_activation,
            reference=f"object-{index:04d}",
        )
        for index in range(1025)
    )
    with pytest.raises(ValidationError):
        _mutation(recovery_objects=objects)


def test_acknowledgement_has_separate_quiescence_digest() -> None:
    acknowledgement = AuthorityAcknowledgementV1(
        authority_id=uuid4(),
        generation=1,
        system_id=uuid4(),
        journal_sequence=2,
        journal_digest=_DIGEST,
        positive_quiescence_digest=_OTHER_DIGEST,
    )
    assert acknowledgement.journal_digest != acknowledgement.positive_quiescence_digest


def test_observation_is_bounded_and_closed() -> None:
    observation = AuthorityObservationV1(
        observation_id=uuid4(), category="source", composite_state=_DIGEST
    )
    assert observation.category == "source"
    with pytest.raises(ValidationError):
        AuthorityObservationV1.model_validate(
            {"observation_id": uuid4(), "category": "invented", "composite_state": _DIGEST}
        )


def _record(phase: JournalPhase, **changes: object) -> JournalRecordV1:
    values = _binding() | {
        "sequence": 1,
        "previous_digest": "sha256:" + "0" * 64,
        "phase": phase,
        "attempt_id": uuid4(),
    }
    values.update(changes)
    return JournalRecordV1.model_validate(values)


def test_takeover_and_mutation_records_are_disjoint() -> None:
    takeover = _record(JournalPhase.WATERMARK_INSTALLED)
    assert takeover.expected_source_identity is None
    with pytest.raises(ValidationError):
        _record(JournalPhase.WATERMARK_INSTALLED, expected_source_identity=_DIGEST)

    mutation = _record(
        JournalPhase.ADMITTED,
        expected_source_identity=_DIGEST,
        intended_target_identity=_OTHER_DIGEST,
        recovery_objects=(),
    )
    assert mutation.expected_source_identity == _DIGEST
    with pytest.raises(ValidationError):
        _record(JournalPhase.ADMITTED)


def test_record_bytes_and_digest_are_deterministic() -> None:
    record = _record(JournalPhase.WATERMARK_INSTALLED)
    encoded = canonical_record_bytes(record)
    assert encoded == canonical_record_bytes(record)
    assert encoded.endswith(b"}")
    assert not encoded.endswith(b"\n")
    assert record_digest(record) == record_digest(record)
    assert record_digest(record).startswith("sha256:")


def test_serialized_request_is_capped_at_one_mib() -> None:
    with pytest.raises(ValidationError):
        _mutation(operation="x" * 1_048_576)
