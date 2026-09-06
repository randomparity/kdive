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
