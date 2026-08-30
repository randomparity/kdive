"""Provider-host external-boot mutation authority (ADR-0584)."""

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
    JournalPhase,
    JournalRecordV1,
    RecoveryObjectBindingV1,
    canonical_record_bytes,
    record_digest,
)

__all__ = [
    "AuthorityAcknowledgementV1",
    "AuthorityMutationRequestV1",
    "AuthorityObservationV1",
    "AuthorityTakeoverRequestV1",
    "FileAuthorityJournal",
    "JournalPhase",
    "JournalRecordV1",
    "RecoveryObjectBindingV1",
    "canonical_record_bytes",
    "record_digest",
]
