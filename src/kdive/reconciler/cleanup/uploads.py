"""Abandoned upload repair for the reconciler."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, cast, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock

_log = logging.getLogger(__name__)

_UPLOAD_RUN_OWNER_KIND = upload_manifest.RUN_UPLOAD_OWNER
_UPLOAD_INVESTIGATION_OWNER_KIND = upload_manifest.INVESTIGATION_UPLOAD_OWNER
_LOCK_SCOPES: dict[upload_manifest.UploadOwnerKind, LockScope] = {
    _UPLOAD_RUN_OWNER_KIND: LockScope.RUN,
    _UPLOAD_INVESTIGATION_OWNER_KIND: LockScope.INVESTIGATION,
}


@runtime_checkable
class UploadStore(Protocol):
    """The narrow object-store port the upload reaper consumes."""

    def list_prefix(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


async def repair_abandoned_uploads(conn: AsyncConnection, store: UploadStore) -> int:
    """Reap a past-deadline manifest's uncommitted prefix objects, then the manifest.

    The obligation is the same for both owner kinds: a manifest past its deadline. For ``runs``
    that sweeps whether the Run is pre-finalize (a true abandon) or finalized with incomplete
    chunk cleanup (the backstop for a failed post-commit delete, ADR-0104 §7). For
    ``investigations`` it sweeps in every investigation state (ADR-0444, superseding ADR-0441 §6's
    terminal-state gate): ``complete_rootfs_upload`` now rejects a past-deadline finalize, so a
    lapsed window can no longer be finalized and reaping it races nothing legitimate. A
    *finalized* rootfs stays safe on the per-key committed-object skip in
    :func:`reap_one_owner`, not on the owner's state.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT owner_kind, owner_id FROM upload_manifests "
            "WHERE deadline < now() AND owner_kind = ANY(%s)",
            (list(_LOCK_SCOPES),),
        )
        candidates = await cur.fetchall()
    reaped = 0
    for cand in candidates:
        owner_kind = cast(upload_manifest.UploadOwnerKind, cand["owner_kind"])
        if await reap_one_owner(conn, store, owner_kind, cand["owner_id"]):
            reaped += 1
    return reaped


async def reap_one_owner(
    conn: AsyncConnection,
    store: UploadStore,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
) -> bool:
    """Re-validate under the per-owner lock, then prefix-reap and delete the manifest.

    The locked re-read is what declines a manifest whose deadline was renewed since the candidate
    select, and the per-owner lock is the one a finalize also takes — so a reap and a finalize
    serialize rather than overlapping, in either order.
    """
    async with conn.transaction(), advisory_xact_lock(conn, lock_scope_for(owner_kind), owner_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT prefix FROM upload_manifests "
                "WHERE owner_kind = %s AND owner_id = %s AND deadline < now()",
                (owner_kind, owner_id),
            )
            row = await cur.fetchone()
        if row is None:
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


def lock_scope_for(owner_kind: upload_manifest.UploadOwnerKind) -> LockScope:
    """Return the advisory-lock scope for an upload owner kind, failing loud on an unknown one.

    An owner kind the reaper does not recognize must never be locked under a guessed scope — that
    would take a lock no writer of that owner holds and reap under no mutual exclusion at all.
    """
    scope = _LOCK_SCOPES.get(owner_kind)
    if scope is None:
        raise ValueError(f"unsupported upload owner kind: {owner_kind}")
    return scope
