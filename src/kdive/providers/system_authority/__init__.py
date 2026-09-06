"""Provider-neutral authority-owned System operation contracts (ADR-0623)."""

from kdive.providers.system_authority.ports import AuthoritySystemProvider
from kdive.providers.system_authority.protocol import (
    AuthoritySystemAbsenceFacts,
    AuthoritySystemCommitContextV1,
    AuthoritySystemJournalRecordV1,
    AuthoritySystemMarkerV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemProvisionFacts,
    AuthoritySystemProvisionSnapshot,
    AuthoritySystemTakeoverRequestV1,
    canonical_system_authority_bytes,
    system_authority_digest,
)

__all__ = [
    "AuthoritySystemAbsenceFacts",
    "AuthoritySystemCommitContextV1",
    "AuthoritySystemJournalRecordV1",
    "AuthoritySystemMarkerV1",
    "AuthoritySystemMutationRequestV1",
    "AuthoritySystemOperation",
    "AuthoritySystemProvisionFacts",
    "AuthoritySystemProvisionSnapshot",
    "AuthoritySystemProvider",
    "AuthoritySystemTakeoverRequestV1",
    "canonical_system_authority_bytes",
    "system_authority_digest",
]
