"""Cycle-free typed facts for authority-owned System teardown (ADR-0620)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdive.db.external_boot_authority_journal import AuthorityBinding
from kdive.domain.external_boot_activation import ExternalBootReleaseEvidenceV1, UtcDateTime
from kdive.providers.ports.external_boot import OpaqueProviderRef

type AuthorityTeardownReservationDisposition = Literal["pending", "ready", "released"]
type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type ProviderRecoveryRefusalReason = Literal[
    "external-boot recovery phase is not resumable",
    "System teardown recovery point does not match retained intent",
    "external-boot pre-stop recovery requires inactive source XML/power",
    "external-boot pre-stop module layout conflicts with metadata",
    # The remote adapter's cleanup, finalize and reap checks (#3436).
    "remote cleanup evidence differs from exact request",
    "remote external-boot identities differ from request",
    "remote external-boot recovery objects are not this activation's",
    "remote cleanup evidence changed before provider deletion",
    "remote cleanup quarantine belongs to another operation",
    "remote cleanup proof differs from the quarantine receipt",
    "remote recovery-object port serves only the recovery record",
    "remote cleanup quarantine ownership conflicts with request",
    "remote cleanup quarantine observation changed before delete",
    "remote cleanup quarantine observation changed before adopt",
    "remote cleanup quarantine receipt names another recovery record",
    "remote cleanup quarantine receipt differs from its owner",
    "remote cleanup finalized marker is malformed",
    "remote cleanup finalized marker conflicts with durable bytes",
    "remote recovery record differs from the quarantine receipt",
    "remote materialization differs from the finalized activation",
    "remote finalize found conflicting preparation requests for one activation; "
    "resolve the duplicate .request records before finalizing",
    "remote module volume remains before authorized cleanup",
    "remote cleanup boot artifact identity differs",
    "remote cleanup volume name differs",
    "remote cleanup volume remained after deletion",
    "remote module installed baseline is incomplete",
    "remote module lifecycle authority differs from preparation",
    "remote module lifecycle baseline identity differs",
    "installed-only module reap requires teardown authority",
    "remote module restored evidence differs from preparation",
    "remote module recovery geometry differs from fixed host binding",
    "remote module restored result differs before reap",
    "remote module reap markers are out of order",
    "remote module reaped marker conflicts with present attempt volumes",
    # The remote adapter's store, teardown, preparation and materialization checks (#3474).
    "remote System teardown record exceeds 1048576 bytes",
    "remote System teardown record is not canonical JSON",
    "remote module preparation request exceeds 1048576 bytes",
    "remote module preparation request is not canonical JSON",
    "provider returned volumes for a different remote module attempt",
    "provider returned mismatched remote module volume identities",
    "remote module preparation response exceeds 1048576 bytes",
    "remote module preparation response is not canonical JSON",
    "remote module terminal recovery differs from exact operation",
    "remote preparation completion is not canonical JSON",
    "remote module lifecycle request is not canonical JSON",
    "remote module lifecycle response is not canonical JSON",
    "persisted remote module lifecycle request is not canonical JSON",
    "remote module lifecycle completion is not canonical JSON",
    "remote module terminal record exceeds 1048576 bytes",
    "remote module terminal record is not canonical JSON",
    "persisted remote module preparation is not canonical JSON",
    "remote cleanup quarantine receipt exceeds 1048576 bytes",
    "remote cleanup quarantine receipt is not canonical JSON",
    "remote cleanup finalized marker exceeds 1048576 bytes",
    "remote cleanup finalized marker is not canonical JSON",
    "remote recovery reference has the wrong namespace",
    "remote recovery reference is malformed",
    "remote preparation evidence exceeds 1048576 bytes",
    "remote preparation evidence conflicts with durable bytes",
    "remote teardown evidence changed before checkpoint",
    "remote System teardown conflicts with retained intent",
    "remote System teardown observation conflicts with retained intent",
    "remote System teardown phase skipped a checkpoint",
    "remote lifecycle evidence conflicts with durable bytes",
    "remote module restored evidence has the wrong action",
    "remote prior-power evidence is malformed",
    "remote module terminal preparation binding differs",
    "remote preparation store exceeds teardown scan bound",
    "remote preparation request name is malformed",
    "remote teardown found conflicting preparation attempts",
    "remote preparation receipt conflicts with durable bytes",
    "published remote preparation receipt failed exact reopen",
    "remote materialization plan index is malformed",
    "remote materialization plan index is incomplete",
    "remote materialization plan index differs",
    "remote teardown plan identity is malformed",
    "remote teardown materialization differs from activation",
    "remote materialization differs from durable record",
    "remote recovery object index is malformed",
    "remote recovery object index is unauthenticated",
    "remote recovery object differs from durable record",
    "remote recovery index is malformed",
    "remote recovery record identity mismatched",
    "remote recovery index differs from durable record",
    "remote recovery point differs from durable record",
    "remote recovery record exceeds 1048576 bytes",
    "remote recovery record is not canonical JSON",
    "remote materialization record exceeds 1048576 bytes",
    "remote materialization record is not canonical JSON",
    "remote materialization binding differs from the requested plan",
    "remote materialization differs from the requested plan",
    "preparation predecessor cannot be adopted",
    "remote preparation returned a different activation",
    "remote preparation request has a non-preparation operation",
    "preparation commit context differs from request",
    "remote external-boot commit context differs from request",
    "remote System teardown context differs from request",
    "remote module teardown host is unavailable",
    "remote conflict resolution observation changed before restore",
    "remote running observation identities differ from request",
    "remote module terminal record differs from activation binding",
    "remote module result omitted captured source manifest",
    "remote recovery omitted an owned private artifact",
    "remote recovery boot artifact identity differs",
    "remote recovery artifact path is not absolute",
    "remote target definition omitted operating-system paths",
    "remote recovery artifact paths differ from target definition",
    "remote teardown boot artifact identity differs",
    "remote teardown domain identity differs from durable recovery",
    "remote teardown cannot validate an absent System domain",
    "remote System domain became active before undefine",
    "remote System domain remains before storage deletion",
    "remote module recovery remains before System storage deletion",
    "remote boot artifact name differs before deletion",
    "remote System volume name differs before deletion",
    "remote recovery-object activation differs from durable record",
    "remote boot-artifact ownership differs",
    "remote module-volume ownership differs",
    "remote recovery record is not a libvirt volume",
    "remote recovery-object readback name differs",
    "remote module-volume geometry differs",
    "remote module volume remains attached",
    "remote recovery-object observation changed before delete",
    "remote recovery object remained after deletion",
    "remote recovery-object observation changed before adoption",
]


class ProviderRecoveryRefusal(ValueError):
    """A kdive-raised recovery or binding refusal whose fixed text may be logged (ADR-0707).

    The reason is a closed set of literals, so the text never carries provider output.
    """

    def __init__(self, reason: ProviderRecoveryRefusalReason) -> None:
        super().__init__(reason)


class SystemTeardownSupersededError(Exception):
    """The retained teardown record belongs to a later generation of the same subject (#2921)."""


class AuthorityTeardownReservationV1(BaseModel):
    """Exact reservation ownership retained before authority-owned host mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disposition: AuthorityTeardownReservationDisposition
    store_identity: OpaqueProviderRef
    owner_key: OpaqueProviderRef
    reserved_bytes: Annotated[int, Field(gt=0)]


class AuthoritySystemTeardownFacts(BaseModel):
    """Path-free provider facts for one exact System teardown request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    intent_identity: Digest
    domain_absent: bool
    overlay_absent: bool
    baseline_absent: bool
    recovery_absent: bool
    quarantine_retained: bool
    completed_at: UtcDateTime | None
    reservation: AuthorityTeardownReservationV1 | None

    @property
    def complete(self) -> bool:
        return (
            self.domain_absent is True
            and self.overlay_absent is True
            and self.baseline_absent is True
            and self.recovery_absent is True
            and self.quarantine_retained is False
            and isinstance(self.completed_at, datetime)
            and self.reservation is not None
        )

    @model_validator(mode="after")
    def _quarantine_matches_completion(self) -> AuthoritySystemTeardownFacts:
        terminal_absence = (
            self.domain_absent is True
            and self.overlay_absent is True
            and self.baseline_absent is True
            and self.recovery_absent is True
            and isinstance(self.completed_at, datetime)
            and self.reservation is not None
        )
        if self.quarantine_retained is terminal_absence:
            raise ValueError("System teardown quarantine must be the inverse of completion")
        return self


@dataclass(frozen=True, slots=True)
class AuthorityTeardownSnapshot:
    """One SQL-authenticated authority binding and its exact reservation ownership."""

    binding: AuthorityBinding
    reservation: AuthorityTeardownReservationV1
    release_identity: str | None
    release_evidence: ExternalBootReleaseEvidenceV1 | None

    def __post_init__(self) -> None:
        binding = self.binding
        if (
            binding.state != "current"
            or binding.purpose != "teardown"
            or binding.operation.value != "teardown"
        ):
            raise ValueError("teardown snapshot authority binding is not current teardown")
        reservation = self.reservation
        release = self.release_evidence
        if reservation.disposition != "released":
            if self.release_identity is not None or release is not None:
                raise ValueError("pending or ready teardown snapshot has release fields")
            return
        if release is None or self.release_identity != release.identity:
            raise ValueError("released teardown snapshot release identity does not match evidence")
        if (
            release.activation_id != binding.activation_id
            or release.system_id != binding.system_id
            or release.store_identity != reservation.store_identity
            or release.owner_key != reservation.owner_key
            or release.reserved_bytes != reservation.reserved_bytes
        ):
            raise ValueError("released teardown snapshot evidence ownership does not match")
