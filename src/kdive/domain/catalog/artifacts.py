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
    """A stored object referenced by a System or Run; write-once.

    ``run_id`` (ADR-0279) is an optional **correlation** attribute, orthogonal to the
    ``(owner_kind, owner_id)`` ownership: a console artifact stays ``owner_kind='systems'``
    and additionally records the id of the Run active during its window. ``None`` means
    uncorrelated (the historical default, written by every non-console insert).
    """

    owner_kind: str
    owner_id: UUID
    object_key: str
    etag: str
    sensitivity: Sensitivity
    retention_class: str
    run_id: UUID | None = None


__all__ = ["Artifact", "Sensitivity"]
