"""Provider-neutral authority-owned System operation contracts (ADR-0623; ADR-0868)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kdive.providers.system_authority.ports import AuthoritySystemProvider
from kdive.providers.system_authority.protocol import (
    AuthoritySystemAbsenceFacts,
    AuthoritySystemAcknowledgementV1,
    AuthoritySystemCommitContextV1,
    AuthoritySystemExecutionV1,
    AuthoritySystemJournalRecordV1,
    AuthoritySystemMarkerV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemPreactivationAbsentV1,
    AuthoritySystemProofV1,
    AuthoritySystemProvisionFacts,
    AuthoritySystemProvisionReadyV1,
    AuthoritySystemProvisionSnapshot,
    AuthoritySystemResponseV1,
    AuthoritySystemRetainedQuarantineV1,
    AuthoritySystemServiceError,
    AuthoritySystemTakeoverRequestV1,
    authority_system_record_digest,
    canonical_system_authority_bytes,
    canonical_system_record_payload,
    make_authority_system_record,
    parse_authority_system_proof,
    system_authority_digest,
)

__all__ = [
    "AuthoritySystemAbsenceFacts",
    "AuthoritySystemAcknowledgementV1",
    "AuthoritySystemCommitContextV1",
    "AuthoritySystemExecutionV1",
    "AuthoritySystemJournalRecordV1",
    "AuthoritySystemMarkerV1",
    "AuthoritySystemMutationRequestV1",
    "AuthoritySystemOperation",
    "AuthoritySystemProvisionFacts",
    "AuthoritySystemPreactivationAbsentV1",
    "AuthoritySystemProofV1",
    "AuthoritySystemProvisionReadyV1",
    "AuthoritySystemProvisionSnapshot",
    "AuthoritySystemResponseV1",
    "AuthoritySystemService",
    "AuthoritySystemServiceError",
    "AuthoritySystemProvider",
    "AuthoritySystemTakeoverRequestV1",
    "AuthoritySystemRetainedQuarantineV1",
    "authority_system_record_digest",
    "canonical_system_authority_bytes",
    "canonical_system_record_payload",
    "make_authority_system_record",
    "parse_authority_system_proof",
    "system_authority_digest",
]


if TYPE_CHECKING:
    from kdive.providers.system_authority.service import AuthoritySystemService


def __getattr__(name: str) -> object:
    if name == "AuthoritySystemService":
        from kdive.providers.system_authority.service import AuthoritySystemService

        return AuthoritySystemService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
