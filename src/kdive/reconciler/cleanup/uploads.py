"""Abandoned upload repair for the reconciler."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import NamedTuple, Protocol, cast, runtime_checkable
from uuid import UUID

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts import upload_manifest
from kdive.artifacts.upload_manifest import UPLOAD_OWNER_KINDS, lock_scope_for
from kdive.db.locks import require_top_level_transaction, try_advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.upload_fences import owner_key_is_fenced

_log = logging.getLogger(__name__)


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

    A **deferred** owner — one whose advisory lock was held when :func:`_claim_abandoned_prefix`
    reached it — is skipped, not waited on (ADR-0510). It is neither a reap nor a failure: it does
    not count toward the return, does not feed the raise, and does not trip the §4 brake. The loop
    continues to the next candidate, which is the whole of #1554: a chunked ``complete_build`` holds
    ``LockScope.RUN`` to request end, so blocking here parked the *entire* sweep — every other
    owner's window included — behind one multi-GiB reassembly.

    The deferral is reported at the end of the pass rather than silently, because the mechanism that
    makes it safe (the next pass re-selects the owner, since its still-past-deadline row is exactly
    what puts it in ``candidates``) is also what makes perpetual starvation invisible: an owner that
    never loses its lock is never reaped, and every pass looks locally fine. The summary carries the
    count and the **oldest** candidate's age past its deadline, measured by Postgres at the
    candidate select, so a starved owner shows up as an age that grows pass over pass.

    Raises:
        CategorizedError: At least one object could not be deleted this pass
            (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`). Raised after the loop, so a partial
            failure never costs a later owner its reap.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT owner_kind, owner_id, now() - deadline AS past_due FROM upload_manifests "
            "WHERE deadline < now() AND owner_kind = ANY(%s)",
            (list(UPLOAD_OWNER_KINDS),),
        )
        candidates = await cur.fetchall()
    reaped = 0
    undeleted = 0
    unclaimed = 0
    deferred: list[timedelta] = []
    for position, cand in enumerate(candidates):
        owner_kind = cast(upload_manifest.UploadOwnerKind, cand["owner_kind"])
        outcome = await reap_one_owner(conn, store, owner_kind, cand["owner_id"])
        if outcome.deferred:
            deferred.append(cast(timedelta, cand["past_due"]))
            continue
        if outcome.reaped:
            reaped += 1
        undeleted += outcome.undeleted
        if outcome.store_refused_everything:
            unclaimed = len(candidates) - position - 1
            break
    if deferred:
        _log.warning(
            "reconciler: upload reap deferred %d of %d past-deadline owner(s) whose lock was held; "
            "the oldest is %s past its deadline. Each is re-selected next pass, but an owner that "
            "never loses its lock is never reaped — an age that keeps growing here is that "
            "starvation (ADR-0510).",
            len(deferred),
            len(candidates),
            max(deferred),
        )
    if undeleted:
        raise CategorizedError(
            f"upload reap could not delete {undeleted} object(s) across {reaped} reaped owner(s); "
            "their manifest rows are already gone, so nothing will rediscover them (ADR-0453). "
            f"Left {unclaimed} candidate(s) unclaimed for the next pass.",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    return reaped


class ReapOutcome(NamedTuple):
    """What one owner's reap did: whether the row went, and how the object sweep fared.

    ``attempted`` counts the keys this sweep tried to delete, which since ADR-0509 is fewer than the
    keys phase 1 doomed: a key the per-key re-check declined is never tried at all.
    ``declined`` carries those separately, because a decline is the guard working rather than a
    fault — it must not raise at the end of the pass and must not trip the ADR-0453 §4 brake.

    ``deferred`` is the *owner*-level counterpart, and the two are not the same event.
    ``declined`` is per key inside a sweep that happened; ``deferred`` says no sweep happened at
    all, because phase 1 found the owner's lock held and did not wait (ADR-0510). Nothing was
    claimed and nothing was deleted, so a deferred outcome carries zeroes everywhere else and the
    manifest row is still there for the next pass to re-select.
    """

    reaped: bool
    deferred: bool
    attempted: int
    declined: int
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
    :func:`_sweep_uncommitted_objects` then re-makes the one decision that can have changed since,
    per key, under that same lock (ADR-0509). What the split cost before that guard existed is
    disclosed in ADR-0453 §Consequences and was filed as #1556 and #1557.

    Returns:
        The :class:`ReapOutcome`. ``reaped`` is ``False`` for the two ways phase 1 produces no
        claim, which are distinguished by ``deferred``: the locked re-read *declined* the owner
        (``deferred`` false — the manifest was gone, or its deadline had been renewed since the
        candidate select), or the owner's lock was held and phase 1 *deferred* it to the next pass
        (``deferred`` true). ``reaped`` is ``True`` however many objects the sweep failed to delete
        or declined, because the row is durably gone and there is nothing left to retry.
        ``attempted``, ``declined`` and ``undeleted`` carry the sweep's fate up to
        :func:`repair_abandoned_uploads`, which reports it once the pass is over and stops claiming
        candidates if a whole owner's sweep was refused.
    """
    claim = await _claim_abandoned_prefix(conn, store, owner_kind, owner_id)
    if claim.keys is None:
        return ReapOutcome(
            reaped=False, deferred=claim.deferred, attempted=0, declined=0, undeleted=0
        )
    doomed = claim.keys
    sweep = await _sweep_uncommitted_objects(conn, store, owner_kind, owner_id, doomed)
    if sweep.undeleted:
        _log.error(
            "reconciler: upload reap left %d of %d object(s) for owner %s/%s undeleted; the "
            "manifest row is already gone, so nothing will rediscover them (ADR-0453)",
            sweep.undeleted,
            len(doomed),
            owner_kind,
            owner_id,
        )
    if sweep.declined:
        _log.info(
            "reconciler: upload reap spared %d of %d object(s) for owner %s/%s; a row, a live "
            "write lease or a held owner lock protects them, so they are the orphan sweep's to "
            "collect (ADR-0509)",
            sweep.declined,
            len(doomed),
            owner_kind,
            owner_id,
        )
    _log.info("reconciler: abandoned upload owner %s/%s reaped", owner_kind, owner_id)
    return ReapOutcome(
        reaped=True,
        deferred=False,
        attempted=sweep.deleted + sweep.undeleted,
        declined=sweep.declined,
        undeleted=sweep.undeleted,
    )


class _Claim(NamedTuple):
    """Phase 1's three outcomes, two of which produce no keys.

    ``keys`` non-``None`` is a claim — the manifest row is committed gone and these objects are
    phase 2's to re-decide. ``keys`` ``None`` with ``deferred`` false is a *decline*: the locked
    re-read found no past-deadline row, so there is nothing to reap. ``keys`` ``None`` with
    ``deferred`` true is a *deferral*: the owner's lock was held, nothing was read or written, and
    the row is still there for the next pass.
    """

    keys: list[str] | None
    deferred: bool


async def _claim_abandoned_prefix(
    conn: AsyncConnection,
    store: UploadStore,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
) -> _Claim:
    """Delete the past-deadline manifest row and return the keys its deletion abandoned.

    The locked re-read is what declines a manifest whose deadline was renewed since the candidate
    select, and the per-owner lock is the one a finalize also takes — so a reap and a finalize
    serialize rather than overlapping, in either order.

    The lock is **attempted, not waited on** (ADR-0510, superseding ADR-0509 §Consequences' "phase 1
    still blocks"). The holder this matters for is the chunked ``complete_build``, which takes
    ``LockScope.RUN`` before its reassembly and deliberately holds it to request end — so a blocking
    acquisition parked this call for a whole multi-GiB reassembly, and with it every *later*
    candidate in :func:`repair_abandoned_uploads`'s serial loop and every repair after it in
    ``_run_repair_plan``, which keeps one pooled connection checked out across the pass. One slow
    finalize therefore delayed the reap of windows belonging to unrelated Runs and investigations,
    which is #1554.

    Deferring costs this owner nothing but a pass. The manifest row is untouched and still past its
    deadline, which is exactly the predicate ``candidates`` selects on, so the next pass re-derives
    it — a deferral defers, it does not drop. What it *can* cost is timeliness without bound if the
    lock is never free, so the deferral is reported: here per owner, and once per pass with the
    oldest age in :func:`repair_abandoned_uploads`.

    The committed-object exemption is computed here, before the objects are deleted, and the row
    delete this commits is what keeps it valid **against the two finalizes**: they are the writers
    the reaped window's own keys have, and both require the manifest row that is gone by the time
    this returns (ADR-0453 §2). The barrier reaches no further, and this is the whole of it:

    - a **re-mint** creates a *new* manifest row, lifting the barrier for the window it opens —
      the keys are owner-addressed, so that window reuses these key names;
    - the listed prefix is the **owner** prefix, not an upload-only namespace, so other run-scoped
      writers (``control.capture_traffic``'s pcap, the vmcore rows) put objects here and commit
      ``artifacts`` rows for them under no manifest at all.

    Both are ADR-0453's second residual (#1557), and neither is this function's to catch: the keys
    it returns are a *proposal*. :func:`_sweep_uncommitted_objects` re-decides each one under this
    same lock immediately before deleting it (ADR-0509), which is where a writer that arrived after
    this commit is seen.

    The claim is logged once the transaction has committed, which is the only record an abort that
    never reaches the sweep leaves behind. It carries the count and the prefix — not because the
    prefix would otherwise be lost (it is ``owner_prefix(_TENANT, owner_kind, owner_id)`` from the
    single mint site, so a leaked window stays enumerable from the owner tables) but because
    *when* and *how many* are not recoverable any other way.

    Returns:
        The :class:`_Claim`: the keys proposed for deletion — every object under the window's prefix
        holding no committed ``artifacts`` row — or no keys, either declined or deferred. An empty
        key list is a reap with nothing to sweep, which is neither of those.
    """
    async with conn.transaction():
        if not await try_advisory_xact_lock(conn, lock_scope_for(owner_kind), owner_id):
            _log.info(
                "reconciler: upload reap deferred owner %s/%s to the next pass; its lock is held, "
                "so a finalize or another reap is active on it (ADR-0510)",
                owner_kind,
                owner_id,
            )
            return _Claim(keys=None, deferred=True)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT prefix FROM upload_manifests "
                "WHERE owner_kind = %s AND owner_id = %s AND deadline < now()",
                (owner_kind, owner_id),
            )
            row = await cur.fetchone()
        if row is None:
            return _Claim(keys=None, deferred=False)
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
    return _Claim(keys=doomed, deferred=False)


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


class _SweepOutcome(NamedTuple):
    """What phase 2 did with one owner's doomed keys — three outcomes, counted apart."""

    deleted: int
    declined: int
    undeleted: int


async def _sweep_uncommitted_objects(
    conn: AsyncConnection,
    store: UploadStore,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
    keys: list[str],
) -> _SweepOutcome:
    """Delete the abandoned window's objects, re-deciding each key under the owner's lock.

    ``keys`` was decided in :func:`_claim_abandoned_prefix`, and it stops being true the instant
    that phase commits. These are keys under the owner's *whole* prefix, not an upload-only
    namespace, so a re-mint, a ``control.capture_traffic`` retry or a vmcore finalize can write one
    of them before this loop reaches it — and deleting the list unconditionally destroyed those
    bytes (#1557). Each key is therefore re-checked here against committed state
    (:func:`~kdive.reconciler.cleanup.upload_fences.owner_key_is_fenced`) **inside the transaction
    that holds the owner's advisory lock and issues the delete**, which is ADR-0509 §1 and the same
    shape ADR-0502 gave the orphan sweep's per-key delete. The lock is the half that makes it a
    closure rather than a narrower window: ``hold_write_lease`` mints under it and
    ``capture_traffic`` holds it across its whole PUT-plus-insert, so a writer either precedes the
    re-check — which then sees its row or its lease — or waits until the delete is already done.

    The lock is attempted, not waited on. A held owner lock means a writer or another reaper is
    active on this owner, so the key is left alone: a reconciler pass has no deadline, and waiting
    would put allocation expiry and orphaned-System repair behind whatever the holder is doing
    (ADR-0455 §5). A declined key is not revisited by the reaper — its manifest row is already gone
    — and does not need to be, because ``repair_leaked_upload_objects`` drains exactly this residue.

    A failed key is logged and skipped rather than raised (ADR-0453 §3): the manifest row is already
    durably gone, so the owner is reaped and there is nothing to retry, and
    :func:`repair_abandoned_uploads` has no per-candidate ``try`` — a raise here would abandon every
    later owner in the pass over one bad key. ``CategorizedError`` is the category the store wraps
    its client and transport errors in; ``psycopg.Error`` joins it now that the re-check needs the
    database, because a key this pass could not decide is a key it could not delete and belongs in
    the same count. Both are caught by type, so a programming error still crashes and cancellation
    still propagates.

    Args:
        conn: An async connection with **no** transaction open — one is opened per key. #1554 was
            closed without fanning this out (ADR-0510): the head-of-line stall it reported was phase
            1 *blocking* on the owner lock, and deferring there removes it while this loop stays
            serial on one connection. Should concurrency ever be revisited, ADR-0509 §Consequences
            still binds — one connection per concurrent worker (a shared one degrades each
            ``transaction()`` to a savepoint that releases no advisory lock), and fan-out across
            *owners*, since every key of one owner contends on the same lock.
        store: The object store to delete through.
        owner_kind: The owner kind whose lock serialises these deletes.
        owner_id: The owner id.
        keys: The keys phase 1 doomed, in listing order.

    Returns:
        The three counts. ``undeleted`` non-zero means those bytes are now unreferenced and, per
        ADR-0453 §Consequences, unswept until the orphan sweep reaches them (#1556).
    """
    deleted = declined = undeleted = 0
    for key in keys:
        try:
            went = await _delete_unless_fenced(conn, store, owner_kind, owner_id, key)
        except (CategorizedError, psycopg.Error) as exc:
            undeleted += 1
            _log.warning("reconciler: upload reap could not delete %s: %s", key, exc)
        else:
            deleted += int(went)
            declined += int(not went)
    return _SweepOutcome(deleted=deleted, declined=declined, undeleted=undeleted)


async def _delete_unless_fenced(
    conn: AsyncConnection,
    store: UploadStore,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
    key: str,
) -> bool:
    """Take the owner's lock, re-check ``key`` against committed state, and delete it under both.

    Returns:
        Whether the object was deleted. ``False`` means the owner lock was held by someone else, or
        a row or a live write lease landed on this key after phase 1 doomed it.
    """
    # A savepoint here would hold the owner lock for the rest of the pass instead of for this one
    # delete, and would release no ``pg_advisory_xact_lock`` at all. ``_run_repair_plan`` hands each
    # repair a freshly pooled connection and every earlier block here commits, so this holds today;
    # it is asserted because nothing at this call site would show if it stopped.
    require_top_level_transaction(conn, "the upload reap's per-key delete")
    async with conn.transaction():
        if not await try_advisory_xact_lock(conn, lock_scope_for(owner_kind), owner_id):
            _log.info(
                "reconciler: upload reap left %s for the orphan sweep; owner %s/%s is locked, so a "
                "writer or another reap is active on it (ADR-0509)",
                key,
                owner_kind,
                owner_id,
            )
            return False
        if await owner_key_is_fenced(conn, owner_kind, owner_id, key):
            _log.info(
                "reconciler: upload reap spared %s; an artifacts row, a re-minted upload window or "
                "a live write lease landed on it after the claim (ADR-0509)",
                key,
            )
            return False
        await asyncio.to_thread(store.delete, key)
    return True
