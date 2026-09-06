"""Closed provider-neutral System authority values (ADR-0623)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.domain.external_boot_activation import UtcDateTime
from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.providers.ports.external_boot import RootSpecV1
from kdive.security.ssh_authorized_key import validate_authorized_public_key

MAX_MESSAGE_BYTES = 1_048_576
MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
GENESIS_DIGEST = "sha256:" + "0" * 64
_PROOF_PREFIX = b"kdive-authority-system-proof-v1\0"

type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type PositiveBigInt = Annotated[int, Field(ge=1, le=MAX_SIGNED_BIGINT)]
type ProviderKind = Literal["local-libvirt", "remote-libvirt"]


def _bounded(value: str, *, maximum: int = 255) -> str:
    if not value or len(value.encode("utf-8")) > maximum or not value.strip():
        raise ValueError(f"value must contain 1 through {maximum} nonblank UTF-8 bytes")
    return value


def canonical_system_authority_bytes(value: BaseModel) -> bytes:
    """Encode one exact bounded authority value as canonical UTF-8 JSON."""
    encoded = json.dumps(
        value.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("authority System value exceeds 1048576 bytes")
    return encoded


def system_authority_digest(value: BaseModel) -> str:
    """Return the domain-separated identity of one closed authority value."""
    return (
        "sha256:"
        + hashlib.sha256(_PROOF_PREFIX + canonical_system_authority_bytes(value)).hexdigest()
    )


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_by_alias=True)

    @model_validator(mode="after")
    def _size_is_bounded(self) -> Self:
        canonical_system_authority_bytes(self)
        return self


class AuthoritySystemOperation(StrEnum):
    PROVISION = "provision"
    PREACTIVATION_TEARDOWN = "preactivation-teardown"


class AuthoritySystemJournalPhase(StrEnum):
    WATERMARK_INSTALLED = "watermark-installed"
    TAKEOVER_SUPERSEDED = "takeover-superseded"
    TAKEOVER_ACKNOWLEDGED = "takeover-acknowledged"
    ADMITTED = "admitted"
    MUTATION_STARTED = "mutation-started"
    PROVIDER_RETURNED = "provider-returned"
    OBSERVED = "observed"
    TERMINAL = "terminal"


class AuthoritySystemMarkerV1(_ClosedValue):
    """Server-derived marker stored on an authority-owned System job."""

    schema_: Literal["authority-system-marker-v1"] = Field(
        "authority-system-marker-v1", alias="schema"
    )
    system_id: UUID
    allocation_id: UUID
    resource_id: UUID
    provider_kind: ProviderKind
    resource_name: str
    authority_instance: str
    profile_identity: Digest
    root_identity: Digest
    operation: AuthoritySystemOperation
    operation_identity: str

    @field_validator("resource_name", "authority_instance", "operation_identity")
    @classmethod
    def _identifiers_are_bounded(cls, value: str) -> str:
        return _bounded(value)


class _AuthoritySystemAttemptBinding(AuthoritySystemMarkerV1):
    schema_: Literal["authority-system-attempt-v1"] = Field(
        "authority-system-attempt-v1", alias="schema"
    )
    authority_id: UUID
    generation: PositiveBigInt
    attempt_id: UUID
    operation_digest: Digest
    bootstrap_identity: Digest


class AuthoritySystemTakeoverRequestV1(_AuthoritySystemAttemptBinding):
    """An allocating candidate that may install a global-chain watermark."""


class AuthoritySystemMutationRequestV1(_AuthoritySystemAttemptBinding):
    """The exact positively acknowledged provider mutation request."""


class AuthoritySystemCommitContextV1(_ClosedValue):
    """Service-owned anchor for one acknowledged mutation."""

    schema_: Literal["authority-system-commit-context-v1"] = Field(
        "authority-system-commit-context-v1", alias="schema"
    )
    attempt_id: UUID
    operation: AuthoritySystemOperation
    journal_sequence: PositiveBigInt
    journal_digest: Digest


class AuthoritySystemObservationV1(_ClosedValue):
    """Path-free identity of one provider observation."""

    category: Literal["owned", "absent", "partial", "conflict", "unreadable"]
    composite_state: Digest


class AuthoritySystemJournalRecordV1(_AuthoritySystemAttemptBinding):
    """One member of the global per-System journal chain."""

    schema_: Literal["authority-system-journal-v1"] = Field(
        "authority-system-journal-v1", alias="schema"
    )
    sequence: PositiveBigInt
    previous_digest: Digest
    phase: AuthoritySystemJournalPhase
    observation: AuthoritySystemObservationV1 | None = None
    outcome: (
        Literal[
            "never-began",
            "provision-ready",
            "provision-failed",
            "preactivation-absent",
            "retained-quarantine",
        ]
        | None
    ) = None
    canonical_record: Digest

    @model_validator(mode="after")
    def _phase_shape_is_closed(self) -> Self:
        if self.phase in {
            AuthoritySystemJournalPhase.WATERMARK_INSTALLED,
            AuthoritySystemJournalPhase.TAKEOVER_SUPERSEDED,
            AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED,
            AuthoritySystemJournalPhase.ADMITTED,
            AuthoritySystemJournalPhase.MUTATION_STARTED,
            AuthoritySystemJournalPhase.PROVIDER_RETURNED,
        } and (self.observation is not None or self.outcome is not None):
            raise ValueError("pre-observation journal phase forbids result evidence")
        if self.phase is AuthoritySystemJournalPhase.OBSERVED and (
            self.observation is None or self.outcome is not None
        ):
            raise ValueError("observed journal phase requires only observation evidence")
        if self.phase is AuthoritySystemJournalPhase.TERMINAL:
            if self.outcome is None:
                raise ValueError("terminal journal phase requires an outcome")
            if (self.outcome == "never-began") != (self.observation is None):
                raise ValueError("terminal observation must match outcome")
            if self.operation is AuthoritySystemOperation.PROVISION and self.outcome not in {
                "never-began",
                "provision-ready",
                "provision-failed",
                "retained-quarantine",
            }:
                raise ValueError("provision terminal outcome is invalid")
            if (
                self.operation is AuthoritySystemOperation.PREACTIVATION_TEARDOWN
                and self.outcome
                not in {"never-began", "preactivation-absent", "retained-quarantine"}
            ):
                raise ValueError("preactivation teardown terminal outcome is invalid")
        return self


class AuthoritySystemProvisionFacts(_ClosedValue):
    """Path-free facts returned by an authority-owned initial provisioner."""

    intent_identity: Digest
    domain_owned: bool
    root_storage_owned: bool
    boot_ready: bool
    bootstrap_ready: bool
    quarantine_retained: bool
    completed_at: UtcDateTime | None

    @property
    def complete(self) -> bool:
        return (
            self.domain_owned is True
            and self.root_storage_owned is True
            and self.boot_ready is True
            and self.bootstrap_ready is True
            and self.quarantine_retained is False
            and isinstance(self.completed_at, datetime)
        )

    @model_validator(mode="after")
    def _completion_is_closed(self) -> Self:
        terminal_owned = (
            self.domain_owned is True
            and self.root_storage_owned is True
            and self.boot_ready is True
            and self.bootstrap_ready is True
            and isinstance(self.completed_at, datetime)
        )
        if self.quarantine_retained is terminal_owned:
            raise ValueError("provision quarantine must be the inverse of completion")
        return self


class AuthoritySystemAbsenceFacts(_ClosedValue):
    """Path-free facts returned by preactivation teardown observation."""

    intent_identity: Digest
    domain_absent: bool
    root_storage_absent: bool
    baseline_absent: bool
    private_intent_absent: bool
    quarantine_retained: bool
    completed_at: UtcDateTime | None

    @property
    def complete(self) -> bool:
        return (
            self.domain_absent is True
            and self.root_storage_absent is True
            and self.baseline_absent is True
            and self.private_intent_absent is True
            and self.quarantine_retained is False
            and isinstance(self.completed_at, datetime)
        )

    @model_validator(mode="after")
    def _completion_is_closed(self) -> Self:
        terminal_absence = (
            self.domain_absent is True
            and self.root_storage_absent is True
            and self.baseline_absent is True
            and self.private_intent_absent is True
            and isinstance(self.completed_at, datetime)
        )
        if self.quarantine_retained is terminal_absence:
            raise ValueError("absence quarantine must be the inverse of completion")
        return self


@dataclass(frozen=True, slots=True)
class AuthoritySystemProvisionSnapshot:
    """Typed DB-derived inputs validated before private provider dispatch."""

    system_id: UUID
    allocation_id: UUID
    resource_id: UUID
    project: str
    provider_kind: ProviderKind
    resource_name: str
    authority_instance: str
    profile: ProvisioningProfile
    profile_identity: str
    source_image_id: UUID
    root_identity: str
    root_spec: RootSpecV1
    bootstrap_public_key: str
    bootstrap_identity: str

    def __post_init__(self) -> None:
        for value in (self.project, self.resource_name, self.authority_instance):
            _bounded(value)
        expected_profile = "sha256:" + profile_digest(self.profile)
        if self.profile_identity != expected_profile:
            raise ValueError("stored provisioning profile identity does not match")
        expected_bootstrap = (
            "sha256:" + hashlib.sha256(self.bootstrap_public_key.encode("utf-8")).hexdigest()
        )
        if self.bootstrap_identity != expected_bootstrap:
            raise ValueError("stored bootstrap public-key identity does not match")
        if validate_authorized_public_key(self.bootstrap_public_key) != self.bootstrap_public_key:
            raise ValueError("stored bootstrap public key is not canonical")
        if self.provider_kind != self.profile.provider.kind.value:
            raise ValueError("stored provider kind does not match provisioning profile")
        if self.root_spec.authority != "stage-inspection":
            raise ValueError("authority System root requires stage inspection")
        if self.root_spec.source.kind != "staged-image":
            raise ValueError("authority System root requires a staged image")
        if self.root_identity != self.root_spec.source.identity:
            raise ValueError("stored root identity does not match RootSpec source")
        if self.profile.arch != self.root_spec.architecture:
            raise ValueError("stored profile and RootSpec architecture do not match")
