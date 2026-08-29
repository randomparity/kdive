"""Durable external-boot activation values (ADR-0583, ADR-0584)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from kdive.domain.capacity.state import (
    ExternalBootActivationState,
    ExternalBootReservationState,
)
from kdive.providers.ports.external_boot import (
    ExternalBootMaterialization,
    OpaqueProviderRef,
    RecoveryPoint,
)

type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

_CANONICAL_VALUE_MAX_BYTES = 65_536


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


type UtcDateTime = Annotated[datetime, AfterValidator(_utc)]


class _ClosedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)
    _identity_prefix: ClassVar[bytes]

    def to_canonical_json(self) -> bytes:
        """Return the compact, sorted UTF-8 encoding used for evidence identity."""
        data = json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        if len(data) > _CANONICAL_VALUE_MAX_BYTES:
            raise ValueError("external-boot canonical value exceeds 65536 bytes")
        return data

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        """Parse canonical evidence, rejecting alternate encodings and oversize values."""
        if len(data) > _CANONICAL_VALUE_MAX_BYTES:
            raise ValueError("external-boot canonical value exceeds 65536 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("external-boot value is not canonical JSON")
        return value

    @property
    def identity(self) -> str:
        payload = self._identity_prefix + b"\0" + self.to_canonical_json()
        return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_refs(values: tuple[OpaqueProviderRef, ...]) -> tuple[OpaqueProviderRef, ...]:
    encoded = [value.to_canonical_json() for value in values]
    if len(encoded) != len(set(encoded)):
        raise ValueError("object references must be duplicate-free")
    if encoded != sorted(encoded):
        raise ValueError("object references must be sorted by canonical bytes")
    return values


class ExternalBootConflictEvidenceV1(_ClosedEvidence):
    _identity_prefix = b"kdive-external-boot-conflict-evidence-v1"

    schema_: Literal["external-boot-conflict-evidence-v1"] = Field(
        "external-boot-conflict-evidence-v1", alias="schema"
    )
    activation_id: UUID
    system_id: UUID
    observation_id: UUID
    composite_state: Digest
    objects: tuple[OpaqueProviderRef, ...]
    observed_at: UtcDateTime

    _objects_are_canonical = field_validator("objects")(_canonical_refs)


class ExternalBootPreRecoveryEvidenceV1(_ClosedEvidence):
    _identity_prefix = b"kdive-external-boot-pre-recovery-evidence-v1"

    schema_: Literal["external-boot-pre-recovery-evidence-v1"] = Field(
        "external-boot-pre-recovery-evidence-v1", alias="schema"
    )
    activation_id: UUID
    system_id: UUID
    run_id: UUID
    plan_identity: Digest
    recovery_object: OpaqueProviderRef
    source_composite_state: Digest
    observed_at: UtcDateTime


class ExternalBootTerminalEvidenceV1(_ClosedEvidence):
    _identity_prefix = b"kdive-external-boot-terminal-evidence-v1"

    schema_: Literal["external-boot-terminal-evidence-v1"] = Field(
        "external-boot-terminal-evidence-v1", alias="schema"
    )
    activation_id: UUID
    system_id: UUID
    outcome: Literal["active", "abandoned", "recovered", "recovery_failed"]
    composite_state: Digest
    objects: tuple[OpaqueProviderRef, ...]
    observed_at: UtcDateTime

    _objects_are_canonical = field_validator("objects")(_canonical_refs)


class ExternalBootReleaseObject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object: OpaqueProviderRef
    absent: Literal[True] = True

    def to_canonical_json(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


def _canonical_release_objects(
    values: tuple[ExternalBootReleaseObject, ...],
) -> tuple[ExternalBootReleaseObject, ...]:
    encoded = [value.to_canonical_json() for value in values]
    if len(encoded) != len(set(encoded)):
        raise ValueError("release objects must be duplicate-free")
    if encoded != sorted(encoded):
        raise ValueError("release objects must be sorted by canonical bytes")
    return values


class ExternalBootReleaseEvidenceV1(_ClosedEvidence):
    _identity_prefix = b"kdive-external-boot-release-evidence-v1"

    schema_: Literal["external-boot-release-evidence-v1"] = Field(
        "external-boot-release-evidence-v1", alias="schema"
    )
    activation_id: UUID
    system_id: UUID
    store_identity: OpaqueProviderRef
    owner_key: OpaqueProviderRef
    reserved_bytes: Annotated[int, Field(gt=0)]
    enumeration_complete: Literal[True] = True
    objects: tuple[ExternalBootReleaseObject, ...]
    verified_at: UtcDateTime

    _objects_are_canonical = field_validator("objects")(_canonical_release_objects)


class ExternalBootTeardownEvidenceV1(_ClosedEvidence):
    _identity_prefix = b"kdive-external-boot-teardown-evidence-v1"

    schema_: Literal["external-boot-teardown-evidence-v1"] = Field(
        "external-boot-teardown-evidence-v1", alias="schema"
    )
    system_id: UUID
    system_state: Literal["torn_down"] = "torn_down"
    observed_at: UtcDateTime


class ExternalBootCleanupEvidenceV1(_ClosedEvidence):
    _identity_prefix = b"kdive-external-boot-cleanup-evidence-v1"

    schema_: Literal["external-boot-cleanup-evidence-v1"] = Field(
        "external-boot-cleanup-evidence-v1", alias="schema"
    )
    activation_id: UUID
    system_id: UUID
    release_identity: Digest
    mode: Literal["ordinary", "system_teardown"]
    teardown_identity: Digest | None = None
    completed_at: UtcDateTime

    @model_validator(mode="after")
    def _mode_matches_teardown(self) -> ExternalBootCleanupEvidenceV1:
        if (self.mode == "system_teardown") != (self.teardown_identity is not None):
            raise ValueError("teardown_identity presence must match cleanup mode")
        return self


class ExternalBootRecoveryAttemptState(StrEnum):
    RECOVERING = "recovering"
    CONFLICT = "conflict"
    FAILED = "failed"
    RECOVERED = "recovered"


type RecoveryBasis = Literal["recovery_point", "pre_recovery"]


class _ClosedRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalBootActivation(_ClosedRow):
    id: UUID
    system_id: UUID
    run_id: UUID
    plan_identity: Digest
    operation_owner_id: UUID
    authority_generation: Annotated[int, Field(gt=0)]
    state: ExternalBootActivationState
    cleanup_complete: bool = False
    activation_readiness_deadline: UtcDateTime | None = None
    materialization: ExternalBootMaterialization | None = None
    recovery_point: RecoveryPoint | None = None
    pre_recovery_evidence: ExternalBootPreRecoveryEvidenceV1 | None = None
    terminal_evidence: ExternalBootTerminalEvidenceV1 | None = None
    teardown_evidence: ExternalBootTeardownEvidenceV1 | None = None
    cleanup_evidence: ExternalBootCleanupEvidenceV1 | None = None
    current_attempt_id: UUID | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _row_invariants(self) -> ExternalBootActivation:
        cleanup_states = {
            ExternalBootActivationState.RECOVERED,
            ExternalBootActivationState.ABANDONED,
            ExternalBootActivationState.RECOVERY_FAILED,
            ExternalBootActivationState.RECOVERY_CONFLICT,
        }
        if self.cleanup_complete and self.state not in cleanup_states:
            raise ValueError("cleanup_complete is invalid for this activation state")
        if (
            self.state
            in {
                ExternalBootActivationState.ACTIVATING,
                ExternalBootActivationState.ACTIVE,
            }
            and self.activation_readiness_deadline is None
        ):
            raise ValueError("activation_readiness_deadline is required")
        return self


class ExternalBootReservation(_ClosedRow):
    activation_id: UUID
    store_identity: Annotated[str, Field(min_length=1, max_length=1024)]
    owner_key: Annotated[str, Field(min_length=1, max_length=1024)]
    reserved_bytes: Annotated[int, Field(gt=0)]
    state: ExternalBootReservationState
    ready_at: UtcDateTime | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _ready_timestamp_matches_state(self) -> ExternalBootReservation:
        if (self.state is ExternalBootReservationState.READY) != (self.ready_at is not None):
            raise ValueError("ready_at presence must match reservation state")
        return self


class ExternalBootReservationRelease(_ClosedRow):
    activation_id: UUID
    store_identity: Annotated[str, Field(min_length=1, max_length=1024)]
    owner_key: Annotated[str, Field(min_length=1, max_length=1024)]
    reserved_bytes: Annotated[int, Field(gt=0)]
    release_identity: Digest
    release_evidence: ExternalBootReleaseEvidenceV1
    teardown_evidence: ExternalBootTeardownEvidenceV1 | None = None
    released_at: UtcDateTime


class ExternalBootRecoveryAttempt(_ClosedRow):
    activation_id: UUID
    attempt_number: Annotated[int, Field(gt=0)]
    attempt_id: UUID
    authority_generation: Annotated[int, Field(gt=0)]
    recovery_basis: RecoveryBasis
    resolution_operation: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    resolution_identity: Digest | None = None
    acknowledged_composite_state: Digest | None = None
    recovery_readiness_deadline: UtcDateTime | None = None
    state: ExternalBootRecoveryAttemptState
    conflict_evidence: ExternalBootConflictEvidenceV1 | None = None
    terminal_evidence: ExternalBootTerminalEvidenceV1 | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _attempt_invariants(self) -> ExternalBootRecoveryAttempt:
        resolution = (
            self.resolution_operation,
            self.resolution_identity,
            self.acknowledged_composite_state,
        )
        if any(value is not None for value in resolution) and not all(
            value is not None for value in resolution
        ):
            raise ValueError("resolution fields must be present together")
        if (
            self.state is ExternalBootRecoveryAttemptState.RECOVERING
            and self.recovery_readiness_deadline is None
        ):
            raise ValueError("recovery_readiness_deadline is required while recovering")
        if (
            self.state is ExternalBootRecoveryAttemptState.CONFLICT
            and self.conflict_evidence is None
        ):
            raise ValueError("conflict evidence is required for a conflict attempt")
        if (
            self.state
            in {
                ExternalBootRecoveryAttemptState.FAILED,
                ExternalBootRecoveryAttemptState.RECOVERED,
            }
            and self.terminal_evidence is None
        ):
            raise ValueError("terminal evidence is required for a terminal attempt")
        return self


__all__ = [
    "ExternalBootActivation",
    "ExternalBootActivationState",
    "ExternalBootCleanupEvidenceV1",
    "ExternalBootConflictEvidenceV1",
    "ExternalBootPreRecoveryEvidenceV1",
    "ExternalBootReleaseEvidenceV1",
    "ExternalBootReleaseObject",
    "ExternalBootReservation",
    "ExternalBootReservationRelease",
    "ExternalBootReservationState",
    "ExternalBootRecoveryAttempt",
    "ExternalBootRecoveryAttemptState",
    "ExternalBootTeardownEvidenceV1",
    "ExternalBootTerminalEvidenceV1",
]
