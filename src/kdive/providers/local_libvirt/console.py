"""Local provider console collaborators (ADR-0864)."""

from __future__ import annotations

import asyncio
from typing import LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.catalog.registration import register_artifact_row
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.db.repositories import ARTIFACTS
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.providers.ports.console import (
    ConsoleSnapshot,
    ConsoleSnapshotReadError,
    ConsoleWindowRead,
)
from kdive.providers.shared.console_evidence import read_redacted_console
from kdive.providers.shared.runtime_paths import console_log_path, read_console_log
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import UNCONFIGURED_OBJECT_STORE
from kdive.store.objectstore import ObjectStore

_CONSOLE_ROW_SQL: LiteralString = (
    "SELECT id, etag FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND object_key = %s"
)
_REFRESH_CONSOLE_ETAG_SQL: LiteralString = "UPDATE artifacts SET etag = %s WHERE id = %s"


class LocalLibvirtConsoleReader:
    """Read cumulative raw local console bytes; control callers own final redaction."""

    async def read_window(
        self, conn: AsyncConnection, system_id: UUID, start_index: int = 0
    ) -> ConsoleWindowRead:
        if start_index != 0:
            raise ValueError("local console reads are cumulative; use start_index=0")
        data = await asyncio.to_thread(read_console_log, console_log_path(system_id))
        return ConsoleWindowRead(data, 0, True)


class LocalLibvirtConsoleSnapshotter:
    """Persist the current boot's redacted local file as a per-Run artifact."""

    def __init__(
        self,
        store: ObjectStore = UNCONFIGURED_OBJECT_STORE,
        *,
        secret_registry: SecretRegistry,
    ) -> None:
        self._store = store
        self._secret_registry = secret_registry

    async def mark_boot_window(self, system_id: UUID) -> int:
        return 0

    async def snapshot(
        self, conn: AsyncConnection, system_id: UUID, run_id: UUID, start_index: int = 0
    ) -> ConsoleSnapshot | None:
        try:
            redacted = await read_redacted_console(system_id, self._secret_registry)
        except CategorizedError as exc:
            raise ConsoleSnapshotReadError(
                str(exc), category=exc.category, details=exc.details, terminal=exc.terminal
            ) from exc
        if redacted is None:
            return None
        stored = await _store_console_artifact(self._store, system_id, run_id, redacted)
        return await _upsert_console_artifact_row(conn, system_id, run_id, stored, redacted)


class _ConsoleRow(NamedTuple):
    id: UUID
    etag: str


async def _existing_console_row(
    conn: AsyncConnection, system_id: UUID, object_key: str
) -> _ConsoleRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_CONSOLE_ROW_SQL, (system_id, object_key))
        row = await cur.fetchone()
    return None if row is None else _ConsoleRow(row["id"], str(row["etag"]))


async def _store_console_artifact(
    artifact_store: ObjectStore, system_id: UUID, run_id: UUID, redacted: bytes
) -> StoredArtifact:
    def _put() -> StoredArtifact:
        return artifact_store.put_artifact(
            ArtifactWriteRequest(
                tenant="local",
                owner_kind="systems",
                owner_id=str(system_id),
                name=f"console-{run_id}",
                data=redacted,
                sensitivity=Sensitivity.REDACTED,
                retention_class="console",
            )
        )

    return await asyncio.to_thread(_put)


async def _upsert_console_artifact_row(
    conn: AsyncConnection,
    system_id: UUID,
    run_id: UUID,
    stored: StoredArtifact,
    redacted: bytes,
) -> ConsoleSnapshot:
    async with conn.transaction():
        existing = await _existing_console_row(conn, system_id, stored.key)
        if existing is None:
            inserted = await ARTIFACTS.insert(
                conn,
                register_artifact_row(
                    stored, owner_kind="systems", owner_id=system_id, run_id=run_id
                ),
            )
            return ConsoleSnapshot(inserted.id, inserted.object_key, redacted)
        if existing.etag != stored.etag:
            await conn.execute(_REFRESH_CONSOLE_ETAG_SQL, (stored.etag, existing.id))
        return ConsoleSnapshot(existing.id, stored.key, redacted)
