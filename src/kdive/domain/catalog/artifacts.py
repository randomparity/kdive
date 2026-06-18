"""Artifact domain vocabulary."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from kdive.domain._records import DomainModel


class Sensitivity(StrEnum):
    """Artifact sensitivity — only a ``redacted`` derivative is response-eligible.

    ``quarantined`` is a raw artifact written before secret registration completed
    (ADR-0075): excluded from every serve gate exactly like ``sensitive``, but marking an
    unfulfilled redaction obligation the op heals to a ``redacted`` sibling before release.
    """

    SENSITIVE = "sensitive"
    REDACTED = "redacted"
    QUARANTINED = "quarantined"


class Artifact(DomainModel):
    """A stored object referenced by a System or Run; write-once."""

    owner_kind: str
    owner_id: UUID
    object_key: str
    etag: str
    sensitivity: Sensitivity
    retention_class: str


__all__ = ["Artifact", "Sensitivity"]
