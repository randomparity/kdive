"""Closed provider-neutral authority and journal values (ADR-0584)."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
MAX_MESSAGE_BYTES = 1_048_576
MAX_RECOVERY_OBJECTS = 1_024
GENESIS_DIGEST = "sha256:" + "0" * 64

type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type PositiveBigInt = Annotated[int, Field(ge=1, le=MAX_SIGNED_BIGINT)]
type Purpose = Literal["activate", "recover", "resolve-conflict", "release", "teardown"]
type ObservationCategory = Literal["source", "target", "mixed", "unreadable", "conflict"]


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
    provider_kind: str
    authority_instance: str
    operation_identity: str
    operation_digest: Digest

    @field_validator("provider_kind", "authority_instance", "operation_identity")
    @classmethod
    def _identifiers_are_bounded(cls, value: str) -> str:
        return _bounded_text(value)


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

    operation: str
    attempt_id: UUID
    expected_source_identity: str
    intended_target_identity: str
    recovery_objects: Annotated[
        tuple[RecoveryObjectBindingV1, ...], Field(max_length=MAX_RECOVERY_OBJECTS)
    ]

    @field_validator("operation")
    @classmethod
    def _operation_is_bounded(cls, value: str) -> str:
        return _bounded_text(value)

    @field_validator("expected_source_identity", "intended_target_identity")
    @classmethod
    def _provider_identity_is_bounded(cls, value: str) -> str:
        return _bounded_text(value, maximum=1024)

    _objects_are_canonical = field_validator("recovery_objects")(_canonical_recovery_objects)


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
    expected_source_identity: str | None = None
    intended_target_identity: str | None = None
    recovery_objects: Annotated[
        tuple[RecoveryObjectBindingV1, ...], Field(max_length=MAX_RECOVERY_OBJECTS)
    ] = ()
    observation: AuthorityObservationV1 | None = None
    outcome: Literal["never-began", "source", "target", "conflict"] | None = None

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
        else:
            if self.predecessor_generation is not None:
                raise ValueError("mutation records forbid predecessor generation")
            if self.expected_source_identity is None or self.intended_target_identity is None:
                raise ValueError("mutation records require source and target identities")
            if any(
                item.system_id != self.system_id or item.activation_id != self.activation_id
                for item in self.recovery_objects
            ):
                raise ValueError("recovery objects must belong to the record binding")
        return self


def canonical_record_bytes(record: JournalRecordV1) -> bytes:
    """Return compact sorted UTF-8 JSON without a trailing newline."""
    return _canonical_bytes(record)


def record_digest(record: JournalRecordV1) -> str:
    """Return the SHA-256 identity of one canonical record."""
    return "sha256:" + hashlib.sha256(canonical_record_bytes(record)).hexdigest()
