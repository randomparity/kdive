"""Abandoned upload repair for the reconciler."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, cast, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from kdive.artifacts import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import RunState, SystemState

_log = logging.getLogger(__name__)

_UPLOAD_RUN_OWNER_KIND = upload_manifest.RUN_UPLOAD_OWNER
_UPLOAD_SYSTEM_OWNER_KIND = upload_manifest.SYSTEM_UPLOAD_OWNER
_UPLOAD_REAPABLE_STATES: dict[upload_manifest.UploadOwnerKind, tuple[str, ...]] = {
    _UPLOAD_RUN_OWNER_KIND: (RunState.CREATED.value,),
    # A `defined` System never entered provisioning (a true abandon); a terminal `failed` System's
    # provision aborted without committing the rootfs `artifacts` row (ADR-0435). Neither is
    # actively reading the staged object, so a past-deadline manifest's uncommitted object +
    # manifest are reapable. `provisioning` is excluded — an in-flight provision may still be
    # reading the object; `ready`/`torn_down` already committed or reclaimed via teardown.
    _UPLOAD_SYSTEM_OWNER_KIND: (SystemState.DEFINED.value, SystemState.FAILED.value),
}
_OWNER_REAPABLE_QUERIES: dict[upload_manifest.UploadOwnerKind, sql.SQL] = {
    _UPLOAD_RUN_OWNER_KIND: sql.SQL("SELECT 1 FROM runs WHERE id = %s AND state = ANY(%s)"),
    _UPLOAD_SYSTEM_OWNER_KIND: sql.SQL("SELECT 1 FROM systems WHERE id = %s AND state = ANY(%s)"),
}


@runtime_checkable
class UploadStore(Protocol):
    """The narrow object-store port the upload reaper consumes."""

    def list_prefix(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


async def repair_abandoned_uploads(conn: AsyncConnection, store: UploadStore) -> int:
    """Reap a past-deadline manifest's uncommitted prefix objects, then the manifest.

    For ``runs`` the obligation is "a Run manifest past its deadline", swept whether the Run is
    pre-finalize (a true abandon) or finalized with incomplete chunk cleanup (the backstop for a
    failed post-commit delete, ADR-0104 §7). The ``systems`` branch reaps a ``defined`` (abandoned)
    or terminal ``failed`` System (ADR-0435), so a provision that failed after staging the uploaded
    rootfs no longer strands its object + manifest; ``provisioning``/``ready`` stay gated out.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT m.owner_kind, m.owner_id FROM upload_manifests m "
            "WHERE m.deadline < now() AND ("
            "  m.owner_kind = %s "
            "  OR (m.owner_kind = %s AND EXISTS ("
            "     SELECT 1 FROM systems s WHERE s.id = m.owner_id AND s.state = ANY(%s))))",
            (
                _UPLOAD_RUN_OWNER_KIND,
                _UPLOAD_SYSTEM_OWNER_KIND,
                list(_UPLOAD_REAPABLE_STATES[_UPLOAD_SYSTEM_OWNER_KIND]),
            ),
        )
        candidates = await cur.fetchall()
    reaped = 0
    for cand in candidates:
        owner_kind = cast(upload_manifest.UploadOwnerKind, cand["owner_kind"])
        scope = LockScope.RUN if owner_kind == _UPLOAD_RUN_OWNER_KIND else LockScope.SYSTEM
        if await reap_one_owner(conn, store, owner_kind, cand["owner_id"], scope):
            reaped += 1
    return reaped


async def reap_one_owner(
    conn: AsyncConnection,
    store: UploadStore,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
    scope: LockScope,
) -> bool:
    """Re-validate under the per-owner lock, then prefix-reap and delete the manifest."""
    async with conn.transaction(), advisory_xact_lock(conn, scope, owner_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT prefix FROM upload_manifests "
                "WHERE owner_kind = %s AND owner_id = %s AND deadline < now()",
                (owner_kind, owner_id),
            )
            row = await cur.fetchone()
        if row is None:
            return False
        # The runs branch reaps a finalized Run's leftover chunks too (ADR-0104 §7); only the
        # systems branch re-checks its reapable-state gate ({defined, failed}, ADR-0435) under lock.
        if owner_kind == _UPLOAD_SYSTEM_OWNER_KIND and not await owner_reapable(
            conn, owner_kind, owner_id
        ):
            return False
        for key in await asyncio.to_thread(store.list_prefix, row["prefix"]):
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT 1 FROM artifacts WHERE object_key = %s", (key,))
                if await cur.fetchone() is None:
                    await asyncio.to_thread(store.delete, key)
        await conn.execute(
            "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
            (owner_kind, owner_id),
        )
    _log.info("reconciler: abandoned upload owner %s/%s reaped", owner_kind, owner_id)
    return True


async def owner_reapable(
    conn: AsyncConnection, owner_kind: upload_manifest.UploadOwnerKind, owner_id: UUID
) -> bool:
    """Report whether the owner is in a state whose past-deadline upload is reapable (ADR-0435)."""
    query = _OWNER_REAPABLE_QUERIES.get(owner_kind)
    if query is None:
        raise ValueError(f"unsupported upload owner kind: {owner_kind}")
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            query,
            (owner_id, list(_UPLOAD_REAPABLE_STATES[owner_kind])),
        )
        return await cur.fetchone() is not None
