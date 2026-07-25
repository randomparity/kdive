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
from kdive.domain.errors import CategorizedError

_log = logging.getLogger(__name__)

# The owner kinds the reaper handles, and the advisory-lock scope each is reaped under. It is
# also the candidate-select filter: the reaper can only reap what it can take the owner's lock on.
_LOCK_SCOPES: dict[upload_manifest.UploadOwnerKind, LockScope] = {
    upload_manifest.RUN_UPLOAD_OWNER: LockScope.RUN,
    upload_manifest.INVESTIGATION_UPLOAD_OWNER: LockScope.INVESTIGATION,
}


@runtime_checkable
class UploadStore(Protocol):
    """The narrow object-store port the upload reaper consumes."""

    def list_prefix(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


async def repair_abandoned_uploads(conn: AsyncConnection, store: UploadStore) -> int:
    """Reap every past-deadline manifest: the row first, then its uncommitted prefix objects.

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
    """Commit the manifest-row delete under the owner's lock, then sweep the window's objects.

    Row-first, and the order is the decision (ADR-0453 §1). Deleting objects inside the
    transaction that deletes the row meant an abort partway through the loop — a store fault, a
    lost connection, cancellation at shutdown — rolled the *row* back with the *bytes* already
    gone, restoring a window byte-identical to the one a finalize had validated. ADR-0448 §2's
    ``_require_unreaped_window`` compares deadline identity, and a rollback re-stamps nothing, so
    the runs single-PUT finalize could not see it and would register ``artifacts`` rows against
    deleted keys.

    The two phases split on that commit: :func:`_claim_abandoned_prefix` makes every decision that
    needs the database, under the lock a finalize also takes; :func:`_sweep_uncommitted_objects`
    then holds neither lock nor connection. What is traded for it is disclosed in ADR-0453
    §Consequences — the objects a failed sweep leaves behind are unreferenced and nothing in this
    tree sweeps that prefix.

    Returns:
        Whether the owner was reaped. ``False`` means the locked re-read declined it (the manifest
        was gone, or its deadline had been renewed since the candidate select). A reap that
        commits is ``True`` however many objects the sweep failed to delete: the row is durably
        gone, so the owner *is* reaped and there is nothing left to retry.
    """
    doomed = await _claim_abandoned_prefix(conn, store, owner_kind, owner_id)
    if doomed is None:
        return False
    await _sweep_uncommitted_objects(store, doomed, owner_kind, owner_id)
    _log.info("reconciler: abandoned upload owner %s/%s reaped", owner_kind, owner_id)
    return True


async def _claim_abandoned_prefix(
    conn: AsyncConnection,
    store: UploadStore,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
) -> list[str] | None:
    """Delete the past-deadline manifest row and return the keys its window abandoned.

    The locked re-read is what declines a manifest whose deadline was renewed since the candidate
    select, and the per-owner lock is the one a finalize also takes — so a reap and a finalize
    serialize rather than overlapping, in either order.

    The committed-object exemption is computed here, before the objects are deleted, and stays
    valid across that gap because the row delete this commits is itself the barrier: the only
    writers that insert an ``artifacts`` row against these keys are the two finalizes, and both
    require the manifest row that is gone by the time this returns (ADR-0453 §2).

    Returns:
        The keys to delete — every object under the window's prefix that holds no committed
        ``artifacts`` row — or ``None`` if the locked re-read declined the owner. An empty list is
        a reap with nothing to sweep, which is not the same as a decline.
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
            return None
        keys = await asyncio.to_thread(store.list_prefix, row["prefix"])
        doomed = await _uncommitted_keys(conn, keys)
        await conn.execute(
            "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
            (owner_kind, owner_id),
        )
    return doomed


async def _uncommitted_keys(conn: AsyncConnection, keys: list[str]) -> list[str]:
    """Return ``keys`` minus those holding a committed ``artifacts`` row, preserving order.

    One set-valued query rather than one per key: the same verdict in a single round trip, which
    keeps the locked phase short now that it is the only phase holding the lock.
    """
    if not keys:
        return []
    async with conn.cursor() as cur:
        await cur.execute("SELECT object_key FROM artifacts WHERE object_key = ANY(%s)", (keys,))
        committed = {row[0] for row in await cur.fetchall()}
    return [key for key in keys if key not in committed]


async def _sweep_uncommitted_objects(
    store: UploadStore,
    keys: list[str],
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
) -> None:
    """Delete the abandoned window's objects, holding no lock and no database connection.

    A failed key is logged and skipped rather than raised (ADR-0453 §3): the manifest row is
    already durably gone, so the owner is reaped and there is nothing to retry, and
    :func:`repair_abandoned_uploads` has no per-candidate ``try`` — a raise here would abandon
    every later owner in the pass over one bad key. ``CategorizedError`` is caught specifically,
    the category the store wraps its client and transport errors in, so a programming error still
    crashes and cancellation still propagates.
    """
    failed = 0
    for key in keys:
        try:
            await asyncio.to_thread(store.delete, key)
        except CategorizedError as exc:
            failed += 1
            _log.warning("reconciler: upload reap could not delete %s: %s", key, exc)
    if failed:
        _log.error(
            "reconciler: upload reap left %d of %d object(s) for owner %s/%s undeleted; the "
            "manifest row is already gone, so nothing will rediscover them (ADR-0453)",
            failed,
            len(keys),
            owner_kind,
            owner_id,
        )


def lock_scope_for(owner_kind: upload_manifest.UploadOwnerKind) -> LockScope:
    """Return the advisory-lock scope for an upload owner kind, failing loud on an unknown one.

    An owner kind the reaper does not recognize must never be locked under a guessed scope — that
    would take a lock no writer of that owner holds and reap under no mutual exclusion at all.
    """
    scope = _LOCK_SCOPES.get(owner_kind)
    if scope is None:
        raise ValueError(f"unsupported upload owner kind: {owner_kind}")
    return scope
