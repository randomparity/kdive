"""Per-Run console snapshot for remote-libvirt (ADR-0235).

The remote console is streamed out-of-band by a reconciler-resident
:class:`~kdive.providers.remote_libvirt.console.collector.ConsoleCollector` into rotating S3
parts. The boot worker cannot reach that in-process collector, so this snapshotter assembles the
System's already-uploaded parts itself and writes an immutable ``console-<run>`` artifact, with its
`artifacts` row committed on the boot handler's connection so it lands atomically with the boot
step. It is best-effort: it reads the parts as of boot completion and may trail the collector's
pump latency (a later boot of the same System still gets its own per-Run key, so no evidence is
overwritten — that is the property this seam exists to guarantee).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import UUID

from kdive.artifacts.storage import StoredArtifact
from kdive.db.repositories import ARTIFACTS
from kdive.providers.ports import ConsoleSnapshot
from kdive.providers.remote_libvirt.console.wiring import RemoteConsolePartStore
from kdive.store.objectstore import object_store_from_env, register_artifact_row

if TYPE_CHECKING:
    from psycopg import AsyncConnection

# The `artifacts` owner_kind for a System-owned object — matches the per-Run object's own key and
# the local boot handler's convention.
_OWNER_KIND = "systems"

_EXISTING_ROW_SQL = (
    "SELECT id, etag FROM artifacts WHERE owner_kind = %s AND owner_id = %s AND object_key = %s"
)
_REFRESH_ETAG_SQL = "UPDATE artifacts SET etag = %s WHERE id = %s"


class RemoteLibvirtConsoleSnapshotter:
    """Assemble the System's S3 console parts into an immutable per-Run console artifact."""

    async def snapshot(
        self, conn: AsyncConnection, system_id: UUID, run_id: UUID
    ) -> ConsoleSnapshot | None:
        """Persist a ``console-<run>`` artifact from the parts captured so far for ``system_id``.

        Returns ``None`` when no console bytes have been streamed yet. The blocking S3 work runs
        in a worker thread; the row is upserted on ``conn`` so it commits with the boot step.
        """
        store = object_store_from_env()
        # The conninfo is unused on this path: this snapshotter writes the per-Run `artifacts` row
        # on the boot handler's `conn` (below), never via the part store's own teardown row path.
        parts = RemoteConsolePartStore(store, "")
        data = await asyncio.to_thread(parts.assemble, system_id)
        if not data:
            return None
        stored = await asyncio.to_thread(parts.put_run_console, system_id, run_id, data)
        artifact_id = await _upsert_run_console_row(conn, system_id, stored)
        return ConsoleSnapshot(artifact_id, stored.key, data)


async def _upsert_run_console_row(
    conn: AsyncConnection, system_id: UUID, stored: StoredArtifact
) -> UUID:
    """Insert the per-Run console row, or refresh its etag if the per-Run key already has one.

    Mirrors the local boot handler's ``_upsert_console_artifact_row``: the per-Run object key is
    unique, so a re-snapshot of the same Run refreshes the etag in place rather than inserting a
    duplicate row.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(_EXISTING_ROW_SQL, (_OWNER_KIND, system_id, stored.key))
            row = await cur.fetchone()
        if row is None:
            inserted = await ARTIFACTS.insert(
                conn, register_artifact_row(stored, owner_kind=_OWNER_KIND, owner_id=system_id)
            )
            return inserted.id
        artifact_id, etag = row
        if str(etag) != stored.etag:
            await conn.execute(_REFRESH_ETAG_SQL, (stored.etag, artifact_id))
        return artifact_id
