"""Abandoned upload repair for the reconciler."""

from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple, Protocol, cast, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)

# The owner kinds the reaper handles, and the advisory-lock scope each is reaped under. It is
# also the candidate-select filter: the reaper can only reap what it can take the owner's lock on.
_LOCK_SCOPES: dict[upload_manifest.UploadOwnerKind, LockScope] = {
    upload_manifest.RUN_UPLOAD_OWNER: LockScope.RUN,
    upload_manifest.INVESTIGATION_UPLOAD_OWNER: LockScope.INVESTIGATION,
}

#: The owner kinds an upload window can be minted for, in reap order. The orphan sweep (ADR-0455)
#: derives the object-store roots it walks from this, so its scope cannot drift from the reaper's.
UPLOAD_OWNER_KINDS: tuple[upload_manifest.UploadOwnerKind, ...] = tuple(_LOCK_SCOPES)


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

    A failed delete is tolerated but bounded twice over (ADR-0453 §3). It is reported by a raise at
    the end of the pass, because ``_run_repair_plan`` puts only a repair that raises into
    ``failures``, which is the only input to the ADR-0190 group-E error counter — swallowing it
    would make a store rejecting every delete report as N successful reaps and zero errors. And a
    whole owner's sweep failing stops the pass claiming further candidates, because each candidate
    claimed under a systemic delete fault costs an irreversible row delete over bytes nothing will
    reclaim (#1556). The candidate select is unbounded, so without that brake one misconfigured
    bucket policy would orphan the entire past-deadline backlog in a single pass, and again every
    30 seconds after.

    Raises:
        CategorizedError: At least one object could not be deleted this pass
            (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`). Raised after the loop, so a partial
            failure never costs a later owner its reap.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT owner_kind, owner_id FROM upload_manifests "
            "WHERE deadline < now() AND owner_kind = ANY(%s)",
            (list(_LOCK_SCOPES),),
        )
        candidates = await cur.fetchall()
    reaped = 0
    undeleted = 0
    unclaimed = 0
    for position, cand in enumerate(candidates):
        owner_kind = cast(upload_manifest.UploadOwnerKind, cand["owner_kind"])
        outcome = await reap_one_owner(conn, store, owner_kind, cand["owner_id"])
        if outcome.reaped:
            reaped += 1
        undeleted += outcome.undeleted
        if outcome.store_refused_everything:
            unclaimed = len(candidates) - position - 1
            break
    if undeleted:
        raise CategorizedError(
            f"upload reap could not delete {undeleted} object(s) across {reaped} reaped owner(s); "
            "their manifest rows are already gone, so nothing will rediscover them (ADR-0453). "
            f"Left {unclaimed} candidate(s) unclaimed for the next pass.",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    return reaped


class ReapOutcome(NamedTuple):
    """What one owner's reap did: whether the row went, and how the object sweep fared."""

    reaped: bool
    attempted: int
    undeleted: int

    @property
    def store_refused_everything(self) -> bool:
        """Every key of a non-empty sweep failed — the signature of a systemic delete fault."""
        return self.attempted > 0 and self.undeleted == self.attempted


async def reap_one_owner(
    conn: AsyncConnection,
    store: UploadStore,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
) -> ReapOutcome:
    """Commit the manifest-row delete under the owner's lock, then sweep the window's objects.

    Row-first, and the order is the decision (ADR-0453 §1): deleting objects inside the transaction
    that deletes the row let an abort mid-loop roll the *row* back with the *bytes* already gone,
    restoring a window ADR-0448 §2's deadline-identity check cannot tell from the one a finalize
    validated. The phases split on that commit — :func:`_claim_abandoned_prefix` makes every
    decision that needs the database, under the lock a finalize also takes, and
    :func:`_sweep_uncommitted_objects` then holds no lock. What the split costs is disclosed in
    ADR-0453 §Consequences and filed as #1556 and #1557.

    Returns:
        The :class:`ReapOutcome`. ``reaped`` is ``False`` when the locked re-read declined the
        owner (the manifest was gone, or its deadline had been renewed since the candidate
        select); it is ``True`` however many objects the sweep failed to delete, because the row
        is durably gone and there is nothing left to retry. ``attempted`` and ``undeleted`` carry
        the sweep's fate up to :func:`repair_abandoned_uploads`, which reports it once the pass is
        over and stops claiming candidates if a whole owner's sweep was refused.
    """
    doomed = await _claim_abandoned_prefix(conn, store, owner_kind, owner_id)
    if doomed is None:
        return ReapOutcome(reaped=False, attempted=0, undeleted=0)
    undeleted = await _sweep_uncommitted_objects(store, doomed)
    if undeleted:
        _log.error(
            "reconciler: upload reap left %d of %d object(s) for owner %s/%s undeleted; the "
            "manifest row is already gone, so nothing will rediscover them (ADR-0453)",
            undeleted,
            len(doomed),
            owner_kind,
            owner_id,
        )
    _log.info("reconciler: abandoned upload owner %s/%s reaped", owner_kind, owner_id)
    return ReapOutcome(reaped=True, attempted=len(doomed), undeleted=undeleted)


async def _claim_abandoned_prefix(
    conn: AsyncConnection,
    store: UploadStore,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
) -> list[str] | None:
    """Delete the past-deadline manifest row and return the keys its deletion abandoned.

    The locked re-read is what declines a manifest whose deadline was renewed since the candidate
    select, and the per-owner lock is the one a finalize also takes — so a reap and a finalize
    serialize rather than overlapping, in either order.

    The committed-object exemption is computed here, before the objects are deleted, and the row
    delete this commits is what keeps it valid **against the two finalizes**: they are the writers
    the reaped window's own keys have, and both require the manifest row that is gone by the time
    this returns (ADR-0453 §2). The barrier reaches no further, and this is the whole of it:

    - a **re-mint** creates a *new* manifest row, lifting the barrier for the window it opens —
      the keys are owner-addressed, so that window reuses these key names;
    - the listed prefix is the **owner** prefix, not an upload-only namespace, so other run-scoped
      writers (``control.capture_traffic``'s pcap, the vmcore rows) put objects here and commit
      ``artifacts`` rows for them under no manifest at all.

    Both are ADR-0453's second residual, filed as #1557. Phase 2 deletes without the ``RUN`` lock
    that used to span the check and the delete, so those writers are no longer serialized against.

    The claim is logged once the transaction has committed, which is the only record an abort that
    never reaches the sweep leaves behind. It carries the count and the prefix — not because the
    prefix would otherwise be lost (it is ``owner_prefix(_TENANT, owner_kind, owner_id)`` from the
    single mint site, so a leaked window stays enumerable from the owner tables) but because
    *when* and *how many* are not recoverable any other way.

    Returns:
        The keys to delete — every object under the window's prefix holding no committed
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
        prefix = cast(str, row["prefix"])
        keys = await asyncio.to_thread(store.list_prefix, prefix)
        doomed = await _uncommitted_keys(conn, keys)
        await upload_manifest.delete_manifest(conn, owner_kind, owner_id)
    _log.info(
        "reconciler: abandoned upload owner %s/%s claimed; sweeping %d object(s) under %s",
        owner_kind,
        owner_id,
        len(doomed),
        prefix,
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


async def _sweep_uncommitted_objects(store: UploadStore, keys: list[str]) -> int:
    """Delete the abandoned window's objects, holding no lock and taking no connection.

    A failed key is logged and skipped rather than raised here (ADR-0453 §3): the manifest row is
    already durably gone, so the owner is reaped and there is nothing to retry, and
    :func:`repair_abandoned_uploads` has no per-candidate ``try`` — a raise here would abandon
    every later owner in the pass over one bad key. It is reported from the count returned here
    instead. ``CategorizedError`` is caught specifically, the category the store wraps its client
    and transport errors in, so a programming error still crashes and cancellation still
    propagates.

    ``keys`` is deleted unconditionally: it was decided in :func:`_claim_abandoned_prefix` and is
    never re-read here. **Anything that lengthens this phase widens #1557** — these are keys under
    the Run's *owner* prefix, so a re-mint, a ``control.capture_traffic`` retry, or a vmcore
    finalize that writes one of them before this loop reaches it has its bytes deleted, and
    neither the owner lock nor the manifest row is held to prevent it. Worth costing before #1554
    fans this out; note also that ``_run_repair_plan`` keeps a pooled connection checked out for
    the whole pass, so this phase is connection-free only in its signature.

    Returns:
        How many of ``keys`` could not be deleted. Non-zero means those bytes are now unreferenced
        and, per ADR-0453 §Consequences, unswept (#1556).
    """
    failed = 0
    for key in keys:
        try:
            await asyncio.to_thread(store.delete, key)
        except CategorizedError as exc:
            failed += 1
            _log.warning("reconciler: upload reap could not delete %s: %s", key, exc)
    return failed


def lock_scope_for(owner_kind: upload_manifest.UploadOwnerKind) -> LockScope:
    """Return the advisory-lock scope for an upload owner kind, failing loud on an unknown one.

    An owner kind the reaper does not recognize must never be locked under a guessed scope — that
    would take a lock no writer of that owner holds and reap under no mutual exclusion at all.
    """
    scope = _LOCK_SCOPES.get(owner_kind)
    if scope is None:
        raise ValueError(f"unsupported upload owner kind: {owner_kind}")
    return scope
