"""Cycle-free typed facts for authority-owned System teardown (ADR-0620)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kdive.db.external_boot_authority_journal import AuthorityBinding
from kdive.domain.external_boot_activation import ExternalBootReleaseEvidenceV1
from kdive.providers.ports.external_boot import OpaqueProviderRef

type AuthorityTeardownReservationDisposition = Literal["pending", "ready", "released"]


@dataclass(frozen=True, slots=True)
class AuthorityTeardownSnapshot:
    """One SQL-authenticated authority binding and its exact reservation ownership."""

    binding: AuthorityBinding
    reservation_disposition: AuthorityTeardownReservationDisposition
    store_identity: OpaqueProviderRef
    owner_key: OpaqueProviderRef
    reserved_bytes: int
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
        if self.reservation_disposition not in {"pending", "ready", "released"}:
            raise ValueError("teardown snapshot reservation disposition is invalid")
        if self.reserved_bytes <= 0:
            raise ValueError("teardown snapshot reserved bytes must be positive")
        release = self.release_evidence
        if self.reservation_disposition != "released":
            if self.release_identity is not None or release is not None:
                raise ValueError("pending or ready teardown snapshot has release fields")
            return
        if release is None or self.release_identity != release.identity:
            raise ValueError("released teardown snapshot release identity does not match evidence")
        if (
            release.activation_id != binding.activation_id
            or release.system_id != binding.system_id
            or release.store_identity != self.store_identity
            or release.owner_key != self.owner_key
            or release.reserved_bytes != self.reserved_bytes
        ):
            raise ValueError("released teardown snapshot evidence ownership does not match")
