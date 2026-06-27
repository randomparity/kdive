"""Artifact catalog row construction helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from kdive.artifacts.storage import StoredArtifact
from kdive.domain.catalog.artifacts import Artifact


def register_artifact_row(stored: StoredArtifact, *, owner_kind: str, owner_id: UUID) -> Artifact:
    """Build the ``artifacts`` row for a stored object (no database access).

    The sensitivity/retention come from ``stored`` so the row matches the object by
    construction. The caller inserts and commits it after the object write
    (ADR-0005 write-before-commit). Timestamps are advisory — the DB overwrites them
    on insert (ADR-0016).
    """
    now = datetime.now(UTC)
    return Artifact(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        owner_kind=owner_kind,
        owner_id=owner_id,
        object_key=stored.key,
        etag=stored.etag,
        sensitivity=stored.sensitivity,
        retention_class=stored.retention_class,
    )
