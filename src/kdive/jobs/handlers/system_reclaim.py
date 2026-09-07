"""Provider-neutral core cleanup after a System provider proves physical absence."""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import LiteralString, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.console.sidecar import sidecar_object_name
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.remote_module_attempt_obligations import RemoteModuleAttemptObligationRepository
from kdive.db.repositories import delete_snapshots_for_system
from kdive.providers.shared.runtime_paths import pcap_dir
from kdive.security.secrets.system_bootstrap_key import delete_system_bootstrap_key
from kdive.store.objectstore import artifact_key

_log = logging.getLogger(__name__)
_CONSOLE_TENANT = "local"
_CONSOLE_PART_LIKE: LiteralString = "%console-part-%"
_SYSRQ_DIAGNOSTIC_LIKE: LiteralString = "%sysrq-diagnostic-%"
_SELECT_ARTIFACT_ROWS_SQL: LiteralString = (
    "SELECT id, object_key FROM artifacts WHERE owner_kind = 'systems' "
    "AND owner_id = %s AND object_key LIKE %s"
)
_DELETE_ARTIFACT_ROW_SQL: LiteralString = "DELETE FROM artifacts WHERE id = %s"


class RetiredKeyBatchDeleter(Protocol):
    """Bounded object-store retirement surface for System teardown artifacts."""

    def delete_retired_key_batch(self, key: str, limit: int) -> bool: ...


async def _system_artifact_rows(
    conn: AsyncConnection, system_id: UUID, object_key_like: LiteralString
) -> list[tuple[UUID, str]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SELECT_ARTIFACT_ROWS_SQL, (system_id, object_key_like))
        return [(row["id"], row["object_key"]) for row in await cur.fetchall()]


async def _reclaim_system_artifact_rows(
    conn: AsyncConnection,
    store: RetiredKeyBatchDeleter,
    system_id: UUID,
    object_key_like: LiteralString,
) -> None:
    for artifact_id, key in await _system_artifact_rows(conn, system_id, object_key_like):
        try:
            complete = await asyncio.to_thread(store.delete_retired_key_batch, key, 20)
        except Exception:  # noqa: BLE001 - teardown must preserve every retryable row
            _log.warning(
                "best-effort retired-key batch for System artifact %s failed; retaining its row",
                key,
                exc_info=True,
            )
            continue
        if not complete:
            _log.info(
                "System artifact %s has more retired versions; retaining its row for retry", key
            )
            continue
        async with conn.transaction():
            await conn.execute(_DELETE_ARTIFACT_ROW_SQL, (artifact_id,))


async def _reclaim_console_artifacts(
    conn: AsyncConnection, store: RetiredKeyBatchDeleter, system_id: UUID
) -> None:
    await _reclaim_system_artifact_rows(conn, store, system_id, _CONSOLE_PART_LIKE)
    sidecar_key = artifact_key(_CONSOLE_TENANT, "systems", str(system_id), sidecar_object_name())
    try:
        complete = await asyncio.to_thread(store.delete_retired_key_batch, sidecar_key, 20)
    except Exception:  # noqa: BLE001 - the System-object sweep owns durable continuation
        _log.warning(
            "best-effort retired-key batch for System sidecar %s failed",
            sidecar_key,
            exc_info=True,
        )
    else:
        if not complete:
            _log.info(
                "System sidecar %s has more retired versions; System sweep will retry", sidecar_key
            )


async def _reclaim_sysrq_artifacts(
    conn: AsyncConnection, store: RetiredKeyBatchDeleter, system_id: UUID
) -> None:
    await _reclaim_system_artifact_rows(conn, store, system_id, _SYSRQ_DIAGNOSTIC_LIKE)


async def reclaim_system_core_after_provider_teardown(
    conn: AsyncConnection,
    artifact_store: RetiredKeyBatchDeleter,
    system_id: UUID,
    *,
    reclaim_snapshot_ledger: bool,
    discharge_mutation_obligations: bool,
) -> None:
    """Reclaim core state only after a provider proves physical System absence."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        if reclaim_snapshot_ledger:
            await delete_snapshots_for_system(conn, system_id)
        if discharge_mutation_obligations:
            await RemoteModuleAttemptObligationRepository().discharge_system_mutation_obligations(
                conn, system_id
            )
    async with conn.transaction():
        await delete_system_bootstrap_key(conn, system_id)
    await asyncio.to_thread(shutil.rmtree, str(pcap_dir(system_id)), ignore_errors=True)
    try:
        await _reclaim_console_artifacts(conn, artifact_store, system_id)
        await _reclaim_sysrq_artifacts(conn, artifact_store, system_id)
    except Exception:  # noqa: BLE001 - reclaim is best-effort; teardown must still succeed
        _log.warning(
            "best-effort System-artifact reclaim for system %s failed",
            system_id,
            exc_info=True,
        )


__all__ = ["RetiredKeyBatchDeleter", "reclaim_system_core_after_provider_teardown"]
