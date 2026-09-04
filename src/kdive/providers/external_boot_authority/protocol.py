"""Closed provider-neutral authority and journal values (ADR-0584)."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
MAX_MESSAGE_BYTES = 1_048_576
MAX_RECOVERY_OBJECTS = 1_024
GENESIS_DIGEST = "sha256:" + "0" * 64

type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type PositiveBigInt = Annotated[int, Field(ge=1, le=MAX_SIGNED_BIGINT)]
type Purpose = Literal["activate", "recover", "resolve-conflict", "release", "teardown"]
type ObservationCategory = Literal["absent", "source", "target", "mixed", "unreadable", "conflict"]


def _bounded_text(value: str, *, maximum: int = 255) -> str:
    if not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"value must contain 1 through {maximum} UTF-8 bytes")
    return value


def _canonical_bytes(value: BaseModel) -> bytes:
    encoded = json.dumps(
        value.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("authority value exceeds 1048576 bytes")
    return encoded


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

    @model_validator(mode="after")
    def _serialized_size_is_bounded(self) -> Self:
        _canonical_bytes(self)
        return self


class AuthorityOperation(StrEnum):
    ACTIVATE = "activate"
    RECOVER = "recover"
    RESOLVE_CONFLICT = "resolve-conflict"
    RELEASE = "release"
    CLEANUP = "cleanup"
    TEARDOWN = "teardown"
    DEADLINE = "deadline"
    RECOVERY_ATTEMPT = "recovery-attempt"
    FAIL = "fail"


_PURPOSE_OPERATIONS: dict[str, frozenset[AuthorityOperation]] = {
    "activate": frozenset(
        {AuthorityOperation.ACTIVATE, AuthorityOperation.DEADLINE, AuthorityOperation.FAIL}
    ),
    "recover": frozenset(
        {
            AuthorityOperation.RECOVER,
            AuthorityOperation.DEADLINE,
            AuthorityOperation.RECOVERY_ATTEMPT,
            AuthorityOperation.FAIL,
        }
    ),
    "resolve-conflict": frozenset({AuthorityOperation.RESOLVE_CONFLICT, AuthorityOperation.FAIL}),
    "release": frozenset(
        {AuthorityOperation.RELEASE, AuthorityOperation.CLEANUP, AuthorityOperation.FAIL}
    ),
    "teardown": frozenset({AuthorityOperation.TEARDOWN, AuthorityOperation.FAIL}),
}


def operation_is_permitted(purpose: str, operation: AuthorityOperation) -> bool:
    """Return whether ``operation`` is a legal commit point for ``purpose``.

    Provider adapters receive the commit point inside ``AuthorityCommitContextV1``, whose
    ``commit_point`` is an ``AuthorityOperation``, so the model layer guarantees the member
    itself. It does not guarantee the two cross-model facts each adapter must still check:
    that the operation is legal for the request's purpose, and that it is the same operation
    the request carries. This exposes the one table rather than letting every adapter copy it.
    """
    return operation in _PURPOSE_OPERATIONS.get(purpose, frozenset())


class _AuthorityBinding(_ClosedValue):
    schema_: Literal["external-boot-authority-v1"] = Field(
        "external-boot-authority-v1", alias="schema"
    )
    authority_id: UUID
    generation: PositiveBigInt
    system_id: UUID
    activation_id: UUID
    run_id: UUID
    plan_identity: Digest
    purpose: Purpose
    operation: AuthorityOperation
    provider_kind: str
    authority_instance: str
    operation_identity: str
    operation_digest: Digest

    @field_validator("provider_kind", "authority_instance", "operation_identity")
    @classmethod
    def _identifiers_are_bounded(cls, value: str) -> str:
        return _bounded_text(value)

    @model_validator(mode="after")
    def _operation_matches_purpose(self) -> Self:
        if self.operation not in _PURPOSE_OPERATIONS[self.purpose]:
            raise ValueError("authority operation is not allowed for its purpose")
        return self


class AuthorityTakeoverRequestV1(_AuthorityBinding):
    """Immutable allocating-authority facts used to install a takeover watermark."""


class RecoveryObjectBindingV1(_ClosedValue):
    """Stable provider recovery-object ownership across authority takeover."""

    system_id: UUID
    activation_id: UUID
    reference: str

    @field_validator("reference")
    @classmethod
    def _reference_is_bounded(cls, value: str) -> str:
        return _bounded_text(value, maximum=1024)


def _canonical_recovery_objects(
    values: tuple[RecoveryObjectBindingV1, ...],
) -> tuple[RecoveryObjectBindingV1, ...]:
    encoded = [_canonical_bytes(value) for value in values]
    if len(encoded) != len(set(encoded)):
        raise ValueError("recovery objects must be duplicate-free")
    if encoded != sorted(encoded):
        raise ValueError("recovery objects must be sorted by canonical bytes")
    return values


class AuthorityMutationRequestV1(_AuthorityBinding):
    """One current-authority provider mutation request."""

    attempt_id: UUID
    expected_source_identity: str
    intended_target_identity: str
    recovery_objects: Annotated[
        tuple[RecoveryObjectBindingV1, ...], Field(max_length=MAX_RECOVERY_OBJECTS)
    ]

    @field_validator("expected_source_identity", "intended_target_identity")
    @classmethod
    def _provider_identity_is_bounded(cls, value: str) -> str:
        return _bounded_text(value, maximum=1024)

    _objects_are_canonical = field_validator("recovery_objects")(_canonical_recovery_objects)

    @model_validator(mode="after")
    def _recovery_objects_belong_to_request(self) -> Self:
        if any(
            item.system_id != self.system_id or item.activation_id != self.activation_id
            for item in self.recovery_objects
        ):
            raise ValueError("recovery object does not belong to request binding")
        return self


type AuthorityRequestV1 = AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1
_AUTHORITY_REQUEST_ADAPTER = TypeAdapter(AuthorityRequestV1)


def _parse_authority_request_bytes(payload: bytes) -> AuthorityRequestV1:
    return _AUTHORITY_REQUEST_ADAPTER.validate_json(payload)


def decode_authority_request(payload: bytes) -> AuthorityRequestV1:
    """Decode one canonical bounded provider-neutral authority request."""
    if type(payload) is not bytes:
        raise TypeError("external-boot authority request must be bytes")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("external-boot authority request exceeds configured byte maximum")
    try:
        request = _parse_authority_request_bytes(payload)
        if _canonical_bytes(request) != payload:
            raise ValueError
    except ValidationError, ValueError:
        raise ValueError("invalid external-boot authority request") from None
    return request


class AuthorityAcknowledgementV1(_ClosedValue):
    """Anchored takeover acknowledgement returned for migration 0122 promotion."""

    schema_: Literal["external-boot-authority-v1"] = Field(
        "external-boot-authority-v1", alias="schema"
    )
    authority_id: UUID
    generation: PositiveBigInt
    system_id: UUID
    journal_sequence: PositiveBigInt
    journal_digest: Digest
    positive_quiescence_digest: Digest


class AuthorityObservationV1(_ClosedValue):
    """Bounded provider state observation."""

    schema_: Literal["external-boot-authority-v1"] = Field(
        "external-boot-authority-v1", alias="schema"
    )
    observation_id: UUID
    category: ObservationCategory
    composite_state: Digest


class JournalPhase(StrEnum):
    WATERMARK_INSTALLED = "watermark-installed"
    TAKEOVER_SUPERSEDED = "takeover-superseded"
    TAKEOVER_ACKNOWLEDGED = "takeover-acknowledged"
    ADMITTED = "admitted"
    MUTATION_STARTED = "mutation-started"
    PROVIDER_RETURNED = "provider-returned"
    OBSERVED = "observed"
    TERMINAL = "terminal"


_TAKEOVER_PHASES = frozenset(
    {
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_SUPERSEDED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    }
)


class JournalRecordV1(_AuthorityBinding):
    """One canonical append-only authority journal record."""

    sequence: PositiveBigInt
    previous_digest: Digest
    phase: JournalPhase
    attempt_id: UUID
    predecessor_generation: PositiveBigInt | None = None
    watermark_sequence: PositiveBigInt | None = None
    watermark_digest: Digest | None = None
    expected_source_identity: str | None = None
    intended_target_identity: str | None = None
    recovery_objects: Annotated[
        tuple[RecoveryObjectBindingV1, ...], Field(max_length=MAX_RECOVERY_OBJECTS)
    ] = ()
    observation: AuthorityObservationV1 | None = None
    outcome: Literal["never-began", "absent", "source", "target", "conflict"] | None = None

    @field_validator("expected_source_identity", "intended_target_identity")
    @classmethod
    def _optional_provider_identity_is_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, maximum=1024)

    _objects_are_canonical = field_validator("recovery_objects")(_canonical_recovery_objects)

    @model_validator(mode="after")
    def _phase_shape_is_closed(self) -> JournalRecordV1:
        has_mutation_fields = (
            self.expected_source_identity is not None
            or self.intended_target_identity is not None
            or bool(self.recovery_objects)
            or self.observation is not None
            or self.outcome is not None
        )
        if self.phase in _TAKEOVER_PHASES:
            if has_mutation_fields:
                raise ValueError("takeover records forbid mutation fields")
            if (self.phase is JournalPhase.TAKEOVER_SUPERSEDED) != (
                self.predecessor_generation is not None
            ):
                raise ValueError("predecessor generation must match takeover supersession")
            watermark_link = (self.watermark_sequence, self.watermark_digest)
            expected_link_presence = self.phase is not JournalPhase.WATERMARK_INSTALLED
            if any(value is not None for value in watermark_link) != expected_link_presence or (
                expected_link_presence and any(value is None for value in watermark_link)
            ):
                raise ValueError("takeover completion must carry the exact watermark link")
        else:
            if any(
                value is not None
                for value in (
                    self.predecessor_generation,
                    self.watermark_sequence,
                    self.watermark_digest,
                )
            ):
                raise ValueError("mutation records forbid takeover linkage")
            if self.expected_source_identity is None or self.intended_target_identity is None:
                raise ValueError("mutation records require source and target identities")
            if any(
                item.system_id != self.system_id or item.activation_id != self.activation_id
                for item in self.recovery_objects
            ):
                raise ValueError("recovery objects must belong to the record binding")
            if self.phase in {
                JournalPhase.ADMITTED,
                JournalPhase.MUTATION_STARTED,
                JournalPhase.PROVIDER_RETURNED,
            } and (self.observation is not None or self.outcome is not None):
                raise ValueError("pre-observation mutation phases forbid result evidence")
            if self.phase is JournalPhase.OBSERVED and (
                self.observation is None or self.outcome is not None
            ):
                raise ValueError("observed records require only observation evidence")
            if self.phase is JournalPhase.TERMINAL:
                if self.outcome is None:
                    raise ValueError("terminal records require an outcome")
                if (self.outcome == "never-began") != (self.observation is None):
                    raise ValueError("terminal observation must match the outcome")
        return self


def canonical_record_bytes(record: JournalRecordV1) -> bytes:
    """Return compact sorted UTF-8 JSON without a trailing newline."""
    return _canonical_bytes(record)


def record_digest(record: JournalRecordV1) -> str:
    """Return the SHA-256 identity of one canonical record."""
    return "sha256:" + hashlib.sha256(canonical_record_bytes(record)).hexdigest()


class AuthorityCommitContextV1(_ClosedValue):
    """Service-constructed proof of the anchored ``mutation-started`` record (ADR-0592).

    Carried across the ``AuthorityMutationAdapter`` seam so a provider adapter can tie its own
    commit to the exact authority journal record without reading the journal itself.

    Provenance differs per field, and the distinction is the point of the value:

    - ``journal_sequence`` and ``journal_digest`` are **service-owned**. They are the anchored
      record's own sequence and digest, computed here, and are unreachable from
      ``AuthorityMutationRequestV1`` — which carries no journal field and is closed — so a
      peer cannot assert a journal position it did not cause.
    - ``commit_point`` and ``phase`` are **pinned**: the phase to a single literal, and the
      commit point to the operation the anchored record carries.
    - ``operation_identity`` and ``attempt_id`` are **peer-sent values that round-trip through
      the anchored record**. ``_binding_matches`` requires the identity to equal the trusted
      binding before the record is anchored, so it is constrained; ``attempt_id`` is
      peer-chosen and carried, not verified. Neither is an authenticity token, and a
      downstream proof must not treat them as one.
    """

    schema_: Literal["external-boot-authority-v1"] = Field(
        "external-boot-authority-v1", alias="schema"
    )
    commit_point: AuthorityOperation
    operation_identity: str
    attempt_id: UUID
    journal_sequence: PositiveBigInt
    journal_digest: Digest
    phase: Literal[JournalPhase.MUTATION_STARTED] = JournalPhase.MUTATION_STARTED

    @field_validator("operation_identity")
    @classmethod
    def _identity_is_bounded(cls, value: str) -> str:
        return _bounded_text(value)

    @classmethod
    def for_record(cls, record: JournalRecordV1) -> AuthorityCommitContextV1:
        """Build the context for one anchored record, refusing every other phase.

        The phase gate is what makes a downstream proof's ``mutation-started`` claim proven
        rather than assumed: a ``provider-returned`` or ``observed`` record describes a
        mutation that already reached the provider, and cannot authorize one that has not.
        """
        if record.phase is not JournalPhase.MUTATION_STARTED:
            raise ValueError("commit context requires an anchored mutation-started record")
        return cls(
            commit_point=record.operation,
            operation_identity=record.operation_identity,
            attempt_id=record.attempt_id,
            journal_sequence=record.sequence,
            journal_digest=record_digest(record),
        )
