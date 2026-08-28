"""Abandoned upload repair for the reconciler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import NamedTuple, Protocol, cast, runtime_checkable
from uuid import UUID

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.storage import VersionBatch, VersionPage
from kdive.artifacts.uploads import upload_manifest
from kdive.artifacts.uploads.upload_manifest import UPLOAD_OWNER_KINDS, lock_scope_for
from kdive.db.locks import require_top_level_transaction, try_advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.uploads.upload_fences import owner_key_is_fenced

_log = logging.getLogger(__name__)


@runtime_checkable
class UploadStore(Protocol):
    """The narrow object-store port the upload reaper consumes."""

    def iter_prefix_version_pages(self, prefix: str) -> Iterator[VersionPage]: ...
    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch: ...
    def delete_batch(self, batch: VersionBatch) -> bool: ...


_REAP_VERSIONS_PER_KEY = 20


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

    Store failures are reported by one raise after the pass so the ADR-0190 group-E error counter
    observes them. A whole owner refusing every attempted batch still stops new claims for that
    pass; already-retired prefixes remain durable input to version-aware orphan repair.

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
        CategorizedError: At least one key batch could not be inventoried or deleted this pass
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
            f"upload reap could not delete {undeleted} key batch(es) across {reaped} reaped "
            "owner(s); "
            "their manifest rows are already gone, so upload orphan repair must rediscover them. "
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

    Phase 1 removes only the manifest row under the owner lock. After that commit, phase 2
    inventories exact versions and captures one bounded batch per key. It rechecks each captured
    key under a fresh short owner transaction, commits, and only then deletes the immutable batch.
    A crash or store failure after phase 1 leaves the prefix visible to upload orphan repair.

    Returns:
        The :class:`ReapOutcome`. ``reaped`` is ``False`` for the two ways phase 1 produces no
        claim, which are distinguished by ``deferred``: the locked re-read *declined* the owner
        (``deferred`` false — the manifest was gone, or its deadline had been renewed since the
        candidate select), or the owner's lock was held and phase 1 *deferred* it to the next pass
        (``deferred`` true). ``reaped`` is ``True`` however many batches failed or were declined,
        because the row is durably gone and the orphan sweep owns every survivor.
        ``attempted``, ``declined`` and ``undeleted`` carry the sweep's fate up to
        :func:`repair_abandoned_uploads`, which reports it once the pass is over and stops claiming
        candidates if a whole owner's sweep was refused.
    """
    claim = await _claim_abandoned_prefix(conn, owner_kind, owner_id)
    if claim.prefix is None:
        return ReapOutcome(
            reaped=False, deferred=claim.deferred, attempted=0, declined=0, undeleted=0
        )
    try:
        doomed = await _first_page_version_keys(store, claim.prefix)
    except CategorizedError as exc:
        _log.warning(
            "reconciler: upload reap could not list versions under %s for owner %s/%s: %s; "
            "the manifest row is already gone and orphan repair will retry the prefix",
            claim.prefix,
            owner_kind,
            owner_id,
            exc,
        )
        return ReapOutcome(reaped=True, deferred=False, attempted=1, declined=0, undeleted=1)
    _log.info(
        "reconciler: abandoned upload owner %s/%s claimed; sweeping %d key(s) under %s",
        owner_kind,
        owner_id,
        len(doomed),
        claim.prefix,
    )
    sweep = await _sweep_uncommitted_objects(conn, store, owner_kind, owner_id, doomed)
    if sweep.undeleted:
        _log.error(
            "reconciler: upload reap left %d of %d key batch(es) for owner %s/%s undeleted; the "
            "manifest row is already gone and upload orphan repair will rediscover them",
            sweep.undeleted,
            len(doomed),
            owner_kind,
            owner_id,
        )
    if sweep.declined:
        _log.info(
            "reconciler: upload reap spared %d of %d key(s) for owner %s/%s; a row, a live "
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
    """Phase 1's three outcomes, two of which produce no prefix.

    ``prefix`` non-``None`` is a claim — the manifest row is committed gone and its versions are
    phase 2's to enumerate. ``prefix`` ``None`` with ``deferred`` false is a *decline*: the locked
    re-read found no past-deadline row, so there is nothing to reap. ``prefix`` ``None`` with
    ``deferred`` true is a *deferral*: the owner's lock was held, nothing was read or written, and
    the row is still there for the next pass.
    """

    prefix: str | None
    deferred: bool


async def _claim_abandoned_prefix(
    conn: AsyncConnection,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
) -> _Claim:
    """Delete the past-deadline manifest row and return its prefix after commit.

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

    This phase deliberately performs no store I/O. The row delete commits before version inventory,
    so a crash or listing fault leaves every survivor visible to upload orphan repair. Committed
    object, re-mint, and live-lease exemptions are rechecked per captured key in phase 2.

    Returns:
        The :class:`_Claim`: the committed rowless prefix, or no prefix when declined or deferred.
    """
    async with conn.transaction():
        if not await try_advisory_xact_lock(conn, lock_scope_for(owner_kind), owner_id):
            _log.info(
                "reconciler: upload reap deferred owner %s/%s to the next pass; its lock is held, "
                "so a finalize or another reap is active on it (ADR-0510)",
                owner_kind,
                owner_id,
            )
            return _Claim(prefix=None, deferred=True)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT prefix FROM upload_manifests "
                "WHERE owner_kind = %s AND owner_id = %s AND deadline < now()",
                (owner_kind, owner_id),
            )
            row = await cur.fetchone()
        if row is None:
            return _Claim(prefix=None, deferred=False)
        prefix = cast(str, row["prefix"])
        await upload_manifest.delete_manifest(conn, owner_kind, owner_id)
    return _Claim(prefix=prefix, deferred=False)


async def _first_page_version_keys(store: UploadStore, prefix: str) -> list[str]:
    """Return unique keys from one bounded version page after the manifest commit.

    The version-aware orphan sweep owns every later page. Keeping this serial phase to one store
    page prevents a version-heavy expired owner from delaying unrelated owners in the same pass.
    """
    pages = store.iter_prefix_version_pages(prefix)
    page = await asyncio.to_thread(_next_version_page, pages)
    if page is None:
        return []
    seen: set[str] = set()
    keys: list[str] = []
    for entry in page.entries:
        if entry.key not in seen:
            seen.add(entry.key)
            keys.append(entry.key)
    return keys


def _next_version_page(pages: Iterator[VersionPage]) -> VersionPage | None:
    """Advance one blocking version iterator outside the event loop."""
    return next(pages, None)


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
    """Capture abandoned versions, fence each key briefly, then exact-delete after unlock.

    These are keys under the owner's whole prefix, so a re-mint, capture retry, or vmcore finalize
    can publish one before this loop reaches it. Each captured key is rechecked against committed
    state (:func:`~kdive.reconciler.cleanup.uploads.upload_fences.owner_key_is_fenced`) inside a
    short owner-locked transaction. That transaction commits before :meth:`UploadStore.delete_batch`
    runs, so store latency never extends the owner lock. Exact immutable identities make that
    unlock safe: a peer PUT after capture receives a different VersionId and cannot enter the batch.

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
        store: The version-aware object store to capture and exact-delete through.
        owner_kind: The owner kind whose lock serialises these deletes.
        owner_id: The owner id.
        keys: Unique keys enumerated after phase 1 committed, in store order.

    Returns:
        Completed or progressing batches, database-fenced declines, and failed batches.
    """
    deleted = declined = undeleted = 0
    for key in keys:
        try:
            batch = await asyncio.to_thread(
                store.capture_exact_versions, key, _REAP_VERSIONS_PER_KEY
            )
            complete = await _delete_unless_fenced(conn, store, owner_kind, owner_id, batch)
        except (CategorizedError, psycopg.Error) as exc:
            undeleted += 1
            _log.warning("reconciler: upload reap could not delete %s: %s", key, exc)
        else:
            if complete is None:
                declined += 1
            else:
                deleted += 1
                if not complete:
                    _log.info(
                        "reconciler: upload reap processed a bounded version batch for %s; its "
                        "latest and uncaptured history remain for upload orphan repair",
                        key,
                    )
    return _SweepOutcome(deleted=deleted, declined=declined, undeleted=undeleted)


async def _delete_unless_fenced(
    conn: AsyncConnection,
    store: UploadStore,
    owner_kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
    batch: VersionBatch,
) -> bool | None:
    """Fence one captured batch under the owner lock, commit, then exact-delete it.

    Returns:
        ``None`` when the key is declined before deletion; otherwise whether the captured history
        was complete. ``False`` therefore records bounded progress, not a fence decline.
    """
    key = batch.key
    if not batch.targets:
        return None
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
            return None
        if await owner_key_is_fenced(conn, owner_kind, owner_id, key):
            _log.info(
                "reconciler: upload reap spared %s; an artifacts row, a re-minted upload window or "
                "a live write lease landed on it after the claim (ADR-0509)",
                key,
            )
            return None
    return await asyncio.to_thread(store.delete_batch, batch)
