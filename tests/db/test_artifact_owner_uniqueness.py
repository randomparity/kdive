"""Database backstop and conflict-aware artifact claims (ADR-0528, #1750)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import ARTIFACTS, ArtifactClaimConflict
from kdive.domain.catalog.artifacts import Artifact, Sensitivity

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _artifact(owner_id: UUID, *, row_id: UUID | None = None, etag: str = "etag-1") -> Artifact:
    return Artifact(
        id=row_id or uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        owner_kind="systems",
        owner_id=owner_id,
        object_key=f"local/systems/{owner_id}/console-part-0000-0001.gz",
        etag=etag,
        sensitivity=Sensitivity.REDACTED,
        retention_class="console",
    )


def test_database_rejects_duplicate_artifact_owner_triple(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            owner_id = uuid4()
            await ARTIFACTS.insert(conn, _artifact(owner_id))
            with pytest.raises(psycopg.errors.UniqueViolation):
                await ARTIFACTS.insert(conn, _artifact(owner_id, etag="etag-2"))

    asyncio.run(_run())


def test_claim_adopts_the_unchanged_database_winner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            owner_id = uuid4()
            first = _artifact(owner_id)
            inserted, was_inserted = await ARTIFACTS.claim(conn, first)
            adopted, adopted_was_inserted = await ARTIFACTS.claim(
                conn, _artifact(owner_id, etag="loser-etag")
            )

            assert was_inserted is True
            assert inserted.id == first.id
            assert adopted_was_inserted is False
            assert adopted.id == first.id
            assert adopted.etag == "etag-1"

    asyncio.run(_run())


def test_claim_bounds_a_repeatedly_disappearing_winner() -> None:
    class _MissingCursor:
        executes = 0

        async def __aenter__(self) -> _MissingCursor:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, *args: object) -> None:
            self.executes += 1

        async def fetchone(self) -> None:
            return None

    class _MissingConnection:
        def __init__(self) -> None:
            self.cursor_instance = _MissingCursor()

        def cursor(self, **kwargs: object) -> _MissingCursor:
            return self.cursor_instance

    async def _run() -> None:
        conn = _MissingConnection()
        with pytest.raises(ArtifactClaimConflict, match="conditionally discard"):
            await ARTIFACTS.claim(cast(Any, conn), _artifact(uuid4()))
        assert conn.cursor_instance.executes == 4

    asyncio.run(_run())
