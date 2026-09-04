"""Closed provider-host authority protocol tests (ADR-0584, #2126)."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kdive.providers.external_boot_authority import protocol
from kdive.providers.external_boot_authority.protocol import (
    MAX_MESSAGE_BYTES,
    MAX_SIGNED_BIGINT,
    AuthorityAcknowledgementV1,
    AuthorityCommitContextV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
    JournalPhase,
    JournalRecordV1,
    RecoveryObjectBindingV1,
    canonical_record_bytes,
    decode_authority_request,
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
        "operation": "recover",
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
    values = _binding() | {
        "operation": "recovery-attempt",
        "attempt_id": uuid4(),
        "expected_source_identity": _DIGEST,
        "intended_target_identity": _OTHER_DIGEST,
    }
    values.update(changes)
    values.setdefault(
        "recovery_objects",
        (
            RecoveryObjectBindingV1(
                system_id=UUID(str(values["system_id"])),
                activation_id=UUID(str(values["activation_id"])),
                reference="recovery/object-a",
            ),
        ),
    )
    return AuthorityMutationRequestV1.model_validate(values)


@pytest.mark.parametrize(
    ("purpose", "operation"),
    [
        ("activate", "activate"),
        ("activate", "deadline"),
        ("activate", "fail"),
        ("recover", "recover"),
        ("recover", "deadline"),
        ("recover", "recovery-attempt"),
        ("recover", "fail"),
        ("resolve-conflict", "resolve-conflict"),
        ("resolve-conflict", "fail"),
        ("release", "release"),
        ("release", "cleanup"),
        ("release", "fail"),
        ("teardown", "teardown"),
        ("teardown", "fail"),
    ],
)
def test_every_authorized_purpose_operation_pair_is_accepted(purpose: str, operation: str) -> None:
    _takeover(purpose=purpose, operation=operation)


@pytest.mark.parametrize("operation", ["unknown", "cleanup"])
def test_unknown_or_cross_purpose_operation_is_rejected(operation: str) -> None:
    with pytest.raises(ValidationError):
        _takeover(purpose="activate", operation=operation)


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
    owner = _binding()
    first = RecoveryObjectBindingV1(
        system_id=UUID(str(owner["system_id"])),
        activation_id=UUID(str(owner["activation_id"])),
        reference="a",
    )
    second = RecoveryObjectBindingV1(
        system_id=first.system_id, activation_id=first.activation_id, reference="b"
    )
    changes = owner | {"recovery_objects": (first, second)}
    assert _mutation(**changes).recovery_objects == (first, second)
    with pytest.raises(ValidationError):
        _mutation(**(owner | {"recovery_objects": (second, first)}))
    with pytest.raises(ValidationError):
        _mutation(**(owner | {"recovery_objects": (first, first)}))


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
                "operation": "recover",
                "expected_source_identity": _DIGEST,
                "intended_target_identity": _OTHER_DIGEST,
                "recovery_objects": (foreign,),
            }
        )

    with pytest.raises(ValidationError, match="request binding"):
        _mutation(recovery_objects=(foreign,))


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
    if phase not in {
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_SUPERSEDED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    }:
        values["operation"] = "recover"
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


@pytest.mark.parametrize(
    ("phase", "changes"),
    [
        (JournalPhase.WATERMARK_INSTALLED, {"watermark_sequence": 1}),
        (JournalPhase.TAKEOVER_SUPERSEDED, {"predecessor_generation": 1}),
        (JournalPhase.TAKEOVER_ACKNOWLEDGED, {}),
    ],
)
def test_takeover_phase_linkage_is_exact(phase: JournalPhase, changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _record(phase, **changes)

    if phase is JournalPhase.WATERMARK_INSTALLED:
        assert _record(phase).phase is phase
    else:
        canonical = {"watermark_sequence": 1, "watermark_digest": _DIGEST}
        if phase is JournalPhase.TAKEOVER_SUPERSEDED:
            canonical["predecessor_generation"] = 1
        assert _record(phase, **canonical).phase is phase


@pytest.mark.parametrize(
    ("phase", "changes"),
    [
        (JournalPhase.ADMITTED, {"outcome": "never-began"}),
        (JournalPhase.MUTATION_STARTED, {"observation": "present"}),
        (JournalPhase.PROVIDER_RETURNED, {"outcome": "source"}),
        (JournalPhase.OBSERVED, {}),
        (JournalPhase.OBSERVED, {"observation": "present", "outcome": "source"}),
        (JournalPhase.TERMINAL, {}),
        (JournalPhase.TERMINAL, {"outcome": "source"}),
        (
            JournalPhase.TERMINAL,
            {"outcome": "never-began", "observation": "present"},
        ),
    ],
)
def test_mutation_phase_evidence_is_exact(phase: JournalPhase, changes: dict[str, object]) -> None:
    observation = AuthorityObservationV1(
        observation_id=uuid4(), category="source", composite_state=_DIGEST
    )
    evidence = {key: observation if value == "present" else value for key, value in changes.items()}
    with pytest.raises(ValidationError):
        _record(
            phase,
            expected_source_identity=_DIGEST,
            intended_target_identity=_OTHER_DIGEST,
            recovery_objects=(),
            **evidence,
        )


@pytest.mark.parametrize(
    ("phase", "changes"),
    [
        (JournalPhase.ADMITTED, {}),
        (JournalPhase.MUTATION_STARTED, {}),
        (JournalPhase.PROVIDER_RETURNED, {}),
        (JournalPhase.OBSERVED, {"observation": "present"}),
        (JournalPhase.TERMINAL, {"outcome": "never-began"}),
        (JournalPhase.TERMINAL, {"observation": "present", "outcome": "source"}),
    ],
)
def test_mutation_phase_evidence_accepts_canonical_shape(
    phase: JournalPhase, changes: dict[str, object]
) -> None:
    observation = AuthorityObservationV1(
        observation_id=uuid4(), category="source", composite_state=_DIGEST
    )
    evidence = {key: observation if value == "present" else value for key, value in changes.items()}
    assert (
        _record(
            phase,
            expected_source_identity=_DIGEST,
            intended_target_identity=_OTHER_DIGEST,
            recovery_objects=(),
            **evidence,
        ).phase
        is phase
    )


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


@pytest.mark.parametrize("value", [_takeover(), _mutation()])
def test_raw_request_decoder_accepts_exact_canonical_bounded_bytes(
    value: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1,
) -> None:
    payload = json.dumps(
        value.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert decode_authority_request(payload) == value


def test_raw_request_decoder_rejects_oversize_before_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_called = False

    def forbidden_parser(_payload: bytes) -> object:
        nonlocal parser_called
        parser_called = True
        raise AssertionError("oversized input reached the parser")

    monkeypatch.setattr(protocol, "_parse_authority_request_bytes", forbidden_parser)

    with pytest.raises(ValueError, match="exceeds configured byte maximum"):
        decode_authority_request(b"x" * (MAX_MESSAGE_BYTES + 1))

    assert parser_called is False


@pytest.mark.parametrize("payload", [b"not-json", b'{"schema":"secret"}', b" {}"])
def test_raw_request_decoder_rejects_malformed_without_echo(payload: bytes) -> None:
    with pytest.raises(ValueError) as caught:
        decode_authority_request(payload)

    assert str(caught.value) == "invalid external-boot authority request"
    assert payload.decode(errors="ignore") not in str(caught.value)


def _anchored(phase: JournalPhase, **changes: object) -> JournalRecordV1:
    """One mutation-phase journal record, the shape the service anchors."""
    return _record(
        phase,
        expected_source_identity=_DIGEST,
        intended_target_identity=_OTHER_DIGEST,
        recovery_objects=(),
        **changes,
    )


def test_commit_context_is_built_only_from_an_anchored_mutation_started_record() -> None:
    started = _anchored(JournalPhase.MUTATION_STARTED, sequence=4)
    context = AuthorityCommitContextV1.for_record(started)

    assert context.journal_sequence == started.sequence
    assert context.journal_digest == record_digest(started)
    assert context.operation_identity == started.operation_identity
    assert context.attempt_id == started.attempt_id
    assert context.commit_point is started.operation
    assert context.phase is JournalPhase.MUTATION_STARTED

    for refused in (JournalPhase.ADMITTED, JournalPhase.PROVIDER_RETURNED):
        with pytest.raises(ValueError, match="mutation-started"):
            AuthorityCommitContextV1.for_record(_anchored(refused, sequence=4))


def test_commit_context_refuses_observed_records_and_is_closed() -> None:
    observed = _anchored(
        JournalPhase.OBSERVED,
        sequence=5,
        observation=AuthorityObservationV1(
            observation_id=uuid4(), category="target", composite_state=_DIGEST
        ),
    )
    with pytest.raises(ValueError, match="mutation-started"):
        AuthorityCommitContextV1.for_record(observed)

    values = AuthorityCommitContextV1.for_record(
        _anchored(JournalPhase.MUTATION_STARTED, sequence=5)
    ).model_dump(mode="json", by_alias=True)

    assert AuthorityCommitContextV1.model_validate(values).journal_sequence == 5
    for rejected in ({"extra": "forbidden"}, {"phase": "observed"}, {"commit_point": "not-an-op"}):
        with pytest.raises(ValidationError):
            AuthorityCommitContextV1.model_validate(values | rejected)


def test_the_wire_mutation_request_carries_no_journal_field() -> None:
    # Spelled out rather than compared against ``model_fields`` itself, which would pass
    # whatever the model grew.
    assert set(AuthorityMutationRequestV1.model_fields) == {
        "schema_",
        "authority_id",
        "generation",
        "system_id",
        "activation_id",
        "run_id",
        "plan_identity",
        "purpose",
        "operation",
        "provider_kind",
        "authority_instance",
        "operation_identity",
        "operation_digest",
        "attempt_id",
        "expected_source_identity",
        "intended_target_identity",
        "recovery_objects",
    }

    values = _mutation().model_dump(mode="json", by_alias=True)
    for smuggled in ("journal_sequence", "journal_digest", "phase"):
        with pytest.raises(ValidationError):
            AuthorityMutationRequestV1.model_validate(values | {smuggled: 1})

    payload = json.dumps(
        values | {"journal_sequence": 1}, sort_keys=True, separators=(",", ":")
    ).encode()
    with pytest.raises(ValueError, match="invalid external-boot authority request"):
        decode_authority_request(payload)
