"""Upload-prefix orphan sweep for the reconciler (ADR-0455, #1556).

The reaper is row-first (ADR-0453 §1): it commits the ``upload_manifests`` row delete, then deletes
the window's objects holding no lock and no connection. When that second phase fails partway the
objects survive with **no** manifest row and **no** ``artifacts`` row, and nothing else in this tree
reclaims them — ``gc_expired_build_artifacts`` enumerates ``artifacts`` rows and the only other
prefix-driven orphan scan covers ``images/``. This module is that missing reclaim path.

It is modelled on :func:`kdive.reconciler.cleanup.images.repair_leaked_images`: list a prefix, keep
only what no row can reach, and hold every candidate behind a store-mtime grace evaluated in
Postgres ``now()`` so a just-written object is never raced.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import NamedTuple, Protocol
from uuid import UUID

import psycopg
from psycopg import AsyncConnection

from kdive.artifacts.storage import HeadResult, ObjectListing
from kdive.artifacts.upload_manifest import UPLOAD_TENANT
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.uploads import UPLOAD_OWNER_KINDS, UploadStore

_log = logging.getLogger(__name__)

#: The object-store roots this sweep walks — one per upload owner kind, and both halves of the
#: prefix derived rather than written out here: the tenant from the constant the mint sites share
#: and the kinds from the reaper's own owner-kind table. A sweep whose prefix drifts from the
#: mint's lists nothing and reports a healthy zero forever while the leak resumes, so neither half
#: is a literal. Other tenants (``remote-libvirt``, ``fault-inject``) never hold an upload window.
UPLOAD_ORPHAN_ROOTS: tuple[str, ...] = tuple(
    f"{UPLOAD_TENANT}/{kind}/" for kind in UPLOAD_OWNER_KINDS
)

#: How many candidates one pass may examine **per root**. The reconciler runs its catalog strictly
#: sequentially on one connection with no per-pass deadline, so an unbounded drain — and the first
#: pass after this ships is the largest this code will ever run, against a backlog accumulating
#: since ADR-0453 — would stall allocation expiry, orphaned-System repair, and domain reaping behind
#: it for as long as it took. Each examined candidate costs a HEAD and a query whatever its outcome,
#: so the cost is per key rather than per pass, and neither term is free: the HEAD is a network
#: round trip, and the query's ``artifacts`` anti-join is still a statement per key now that #1570's
#: ``object_key`` btree (migration ``0081``) has made it an index scan rather than a sequential one.
#: The budget is the drain side of the brake ADR-0453 §4 put on the reap side; the remainder is
#: reclaimed 30 seconds later.
#:
#: Since the listing is paged (ADR-0498) the budget bounds the **listing** too: a root stops being
#: paged once its allowance is examined, so a drain no longer enumerates a backlog it could not have
#: acted on. What the budget counts is unchanged — one unit per candidate reaching the re-read.
MAX_RECLAIMS_PER_ROOT = 200

# The number of ``/``-separated components in an upload object key: ``<tenant>/<kind>/<id>/<name>``.
# ``validate_key_component`` rejects ``/`` in every component, so a well-formed key has exactly
# this many and a key with any other shape was not written by the upload lane.
_KEY_COMPONENTS = 4

# One statement decides reclaimability, and it serves both the per-page classify and the per-key
# re-check so the two cannot drift into disagreeing about what is safe to delete (ADR-0455 §2).
# Its four parallel arrays are a listing **page** wide, not a root wide (ADR-0498). The width is
# visible to the planner — ``unnest``'s row estimate tracks the array's real length — so the plan
# does change with it, and it changes toward the index: at a page's width the ``artifacts`` anti-
# join is a nested loop over #1570's ``object_key`` btree, where a root's width tipped it to a
# hash anti-join over a sequential scan. Measured wall-clock time is flat across the widths
# (ADR-0498 §5).
# Every fence is evaluated against Postgres ``now()``, never a Python clock:
#   * no ``artifacts`` row reaches the key — the object is unregistered;
#   * the owner holds no ``upload_manifests`` row *at all* — not merely no live one. A lapsed
#     window is the reaper's to collect, and a live or re-minted window owns these key names
#     because upload keys are owner-addressed;
#   * the object is older than the grace.
_RECLAIMABLE_SQL = """
SELECT c.key
FROM unnest(%s::text[], %s::timestamptz[], %s::text[], %s::uuid[])
     AS c(key, last_modified, owner_kind, owner_id)
WHERE c.last_modified < now() - %s
  AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.object_key = c.key)
  AND NOT EXISTS (SELECT 1 FROM upload_manifests m
                  WHERE m.owner_kind = c.owner_kind AND m.owner_id = c.owner_id)
"""


class UploadOrphanStore(UploadStore, Protocol):
    """The reaper's port plus the two reads the orphan sweep needs.

    ``iter_prefix_pages_with_mtime`` streams a root a page at a time; ``head`` stats one key. They
    are separate methods because they answer separate questions: the sweep needs *every* key under a
    root once per pass, and *one* key's current mtime immediately before each delete. Serving the
    second with the first is what #1575 fixed.

    The listing port is the **paged** one, not the flattened one, because a port that could hand
    this sweep a whole root's listing is a port through which the cost #1569 fixed can return
    (ADR-0498 §1). ``ObjectStore`` still offers ``list_prefix_with_mtime`` for the bounded
    ``images/`` sweep; this port does not.

    An implementation must not do I/O before the returned iterator's first ``next``: the sweep calls
    this method on the event loop and offloads only the advance (:func:`_next_page`). A generator
    function satisfies that by construction, which is what ``ObjectStore`` is; one that eagerly
    fetched a whole root and returned ``iter(...)`` would type-check and block the loop for exactly
    as long as the cost this port exists to bound.
    """

    def iter_prefix_pages_with_mtime(self, prefix: str) -> Iterator[list[ObjectListing]]: ...
    def head(self, key: str) -> HeadResult | None: ...


@dataclass(frozen=True, slots=True)
class UploadOrphanCandidate:
    """One listed object attributed to the upload owner whose prefix it sits under."""

    key: str
    last_modified: datetime
    owner_kind: str
    owner_id: UUID


async def repair_leaked_upload_objects(
    conn: AsyncConnection,
    store: UploadOrphanStore,
    orphan_grace: timedelta,
    upload_ttl: timedelta,
) -> int:
    """Delete objects under the upload roots that no ``artifacts`` or manifest row can reach.

    Walks ``local/runs/`` and ``local/investigations/`` a listing page at a time, attributes each
    listed key to its owner by parsing it, classifies each page in one query, then re-runs that same
    predicate for each reclaimable key immediately before deleting it — so a finalize or a re-mint
    that commits between the listing and the delete protects its object. Nothing is held for a whole
    root: peak memory and the classify's parameter width are bounded by a page (ADR-0498).

    The reclaim threshold is ``orphan_grace + upload_ttl``, and the second term is not padding
    (ADR-0455 §2). The manifest fence protects an object only until the reaper deletes its window's
    row, which happens a TTL after the mint — so a threshold measured on the object's mtime alone
    and merely *equal* to the TTL makes the bytes reclaimable within seconds of the reap, and one
    above it makes them reclaimable in the very pass that reaped them. Summing the two puts the
    threshold a full ``orphan_grace`` past the earliest reap of any window the object could have
    belonged to, at whatever TTL **this process** is configured with. Both terms parse as
    non-negative, so no configured value can invert the cutoff; what remains is deployment skew the
    code cannot see — a reconciler provisioned without ``KDIVE_UPLOAD_TTL_SECONDS`` while the
    minting server raises it — which is the one way the margin can still go negative (ADR-0455 §2).

    A root has four failure sites — its listing, each page's classify, and each per-key re-read and
    delete — and all four are logged, counted, and skipped, with the pass raising once at the end
    (ADR-0455 §5); each root gets its own work budget (§6). The re-read is worth naming separately
    from the delete because since #1575 it is the sweep's only ``head_object``, so it is the one
    call a credential scoped to ``s3:ListBucket`` and ``s3:DeleteObject`` cannot make. Aborting at
    the first failure would let one permanently undeletable object — an S3 Object Lock hold, a
    per-key deny — or one unlistable or unclassifiable prefix starve every candidate behind it and
    the whole second root, on every pass forever, which is the leak this repair exists to drain.
    Raising at the end
    still reaches the ADR-0190 group-E error counter via ``_run_repair_plan``'s ``failures``, and
    nothing is lost by either path: this sweep commits nothing, so the next pass re-derives the
    identical candidates.

    Args:
        conn: An async connection. Each query runs in its own short transaction so no snapshot is
            held across the blocking store calls.
        store: The object store to list and delete through.
        orphan_grace: How long past the earliest possible reap an object is protected.
        upload_ttl: The configured upload-window TTL, added to ``orphan_grace``.

    Returns:
        The number of objects deleted; one INFO line per delete.

    Raises:
        CategorizedError: at least one root or key could not be swept
            (:attr:`~kdive.domain.errors.ErrorCategory.INFRASTRUCTURE_FAILURE`), raised once after
            the last root. Any fault that ends the pass *outright* instead — cancellation at
            shutdown, a bug in this module — propagates unchanged, and takes the same count-logging
            path on the way out, because it arrives after the same irreversible deletes
            (ADR-0455 §5).
    """
    grace = orphan_grace + upload_ttl
    tally = _Tally()
    try:
        for root in UPLOAD_ORPHAN_ROOTS:
            await _sweep_root(conn, store, root, grace, tally)
    except BaseException:  # noqa: BLE001 - logged and re-raised, never swallowed
        # Any abort — a listing fault, a dropped pool connection out of the classify, cancellation
        # at shutdown — can arrive after this pass has already deleted irreversibly, and
        # ``_run_repair_plan`` records no count for a repair that raises. Put the counts on the
        # record before the exception carries them off (ADR-0455 §5).
        tally.log_abort()
        raise
    return tally.reported()


@dataclass
class _Tally:
    """One pass's outcome counts, accumulated across every root it reaches."""

    deleted: int = 0
    failed: int = 0

    def log_abort(self) -> None:
        """Put the counts on the record for a pass that ended before its last root.

        Distinct from :meth:`log` because ``failed`` is normally **zero** here: the faults this
        sweep counts are the ones it recovers from, and an abort is by definition one it did not.
        Reporting "could not reclaim 0" in that state reads as a clean pass, which is the opposite
        of what happened — at least one root was never swept at all.
        """
        _log.error(
            "reconciler: upload orphan sweep aborted before its last root; it had reclaimed %d "
            "object(s) and counted %d it could not reclaim. Neither count reaches the repairs "
            "counter because this pass raises, and a root it had not reached was not swept.",
            self.deleted,
            self.failed,
        )

    def log(self) -> None:
        """Put the counts on the record, because a raising repair reports none.

        ``_run_repair_plan`` records a count only for a repair that *returns*, so a pass that
        deleted irreversibly and then raised shows zero on the ADR-0190 repairs counter. That is
        the trade ADR-0453 §3 already made for the reaper — the count is a gauge, the raise is the
        alert — but here it can pin a working drain's gauge at zero, so the count goes to the log
        rather than being lost with the exception.
        """
        _log.error(
            "reconciler: upload orphan sweep reclaimed %d object(s) and could not reclaim %d; the "
            "reclaimed count is not reported to the repairs counter because this pass raises",
            self.deleted,
            self.failed,
        )

    def reported(self) -> int:
        """Return the reclaimed count, or log it and raise if any key failed."""
        if not self.failed:
            return self.deleted
        self.log()
        raise CategorizedError(
            f"upload orphan sweep could not reclaim {self.failed} object(s); {self.deleted} were "
            "reclaimed this pass. Nothing is lost — the next pass re-derives the same candidates "
            "— but a key that fails every pass (an object-lock hold, a per-key deny) leaks until "
            "it is cleared.",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )


@dataclass
class _RootSweep:
    """One root's in-flight state: its work budget, and the counts the drift warning reads.

    The counts are accumulated across the root's pages rather than taken from one, because what
    ADR-0455 §4 detects is a *root* whose key layout this parser no longer recognizes. Warning per
    page would fire on any single page that happened to hold only unattributable keys, which under a
    paged listing is a page boundary landing badly rather than the drift the warning names.
    """

    root: str
    examined: int = 0
    listed: int = 0
    attributed: int = 0

    @property
    def budget_spent(self) -> bool:
        """Whether this root has examined its whole allowance (ADR-0455 §6)."""
        return self.examined >= MAX_RECLAIMS_PER_ROOT


async def _sweep_root(
    conn: AsyncConnection,
    store: UploadOrphanStore,
    root: str,
    grace: timedelta,
    tally: _Tally,
) -> None:
    """Reclaim one root's orphans a page at a time, spending at most ``MAX_RECLAIMS_PER_ROOT`` work.

    The root is streamed, not materialized (ADR-0498): each ``list_objects_v2`` page is attributed,
    classified, and acted on before the next is fetched, so peak memory and the classify's array
    parameters are bounded by a page and not by a bucket that never shrinks. The budget now also
    stops the **listing**: a drain examines its allowance out of the first page or two and never
    pages the remaining backlog it could not have acted on anyway.

    The budget is **per root**, not per pass, so a persistent fault under `local/runs/` — an object
    lock over a prefix, a scoped ``s3:DeleteObject`` deny — cannot consume the whole allowance on
    failures and leave `local/investigations/` unlisted on every pass forever, which is the
    starvation §5's skip-and-count exists to prevent (ADR-0455 §6). The two faults that end a root
    — its listing and its classify — are skipped and counted for that same reason; see
    :func:`_next_page_or_fault` and :func:`_reclaim_page`.

    It charges every candidate that reaches the re-read, not only the ones deleted: a declined
    re-check costs the same HEAD and query as a delete, and two overlapping passes (the daemon loop
    and an on-demand ``ops.reconcile``) see exactly that — the second finds every object already
    gone and would otherwise re-read the whole backlog uncapped.
    """
    sweep = _RootSweep(root)
    # Abandoned rather than closed on every early exit below. There is nothing to release — the
    # paginator holds no handle — and ``pages.close()`` would be no safer on the one path where
    # finalization is awkward: a ``CancelledError`` out of the ``to_thread`` leaves the generator
    # still advancing on the worker thread, and closing a running generator raises either way.
    pages = store.iter_prefix_pages_with_mtime(root)
    while not sweep.budget_spent:
        page = await _next_page_or_fault(pages, root, tally)
        if page is None:
            break  # the listing is exhausted, or it faulted and was counted
        if not await _reclaim_page(conn, store, page, grace, tally=tally, sweep=sweep):
            break  # the classify faulted and was counted; this root is done
    if sweep.budget_spent:
        _log.info(
            "reconciler: upload orphan sweep stopped at its %d-object budget for %s; any remaining "
            "backlog is reclaimed by the following passes",
            MAX_RECLAIMS_PER_ROOT,
            root,
        )
    _warn_if_wholly_unattributable(root, sweep.listed, sweep.attributed)


async def _next_page_or_fault(
    pages: Iterator[list[ObjectListing]], root: str, tally: _Tally
) -> list[ObjectListing] | None:
    """Pull the next listing page, or count a listing fault and return ``None`` to end the root.

    A listing fault ends this root and not the **pass**: the roots' candidate sets are wholly
    independent, so with no further listing there is nothing left to be partial about here and
    nothing at all is implied about the sibling. Aborting instead would leave
    `local/investigations/`, the rootfs upload lane's root, unswept on every pass for as long as the
    fault lasted — the starvation ADR-0455 §5 skips-and-counts to prevent and §6 makes the budget
    per root to prevent, arriving by the one path neither covers. A scoped ``s3:ListBucket`` deny
    (an IAM ``s3:prefix`` condition is the standard way to scope list authority) is exactly that
    fault, and it is the list-side twin of the per-prefix ``s3:DeleteObject`` deny the budget
    already survives.

    Since the listing is paged, the fault can also arrive **mid-root**, after earlier pages have
    already deleted irreversibly (ADR-0498 §3). Those deletes stand and are counted; the root is
    abandoned from the failed page on. That is strictly more progress than the whole-root listing
    made — it lost every root whose listing faulted at any point — and it is safe for the same
    reason a budget-truncated root is: this sweep commits nothing, so the next pass re-derives the
    identical candidates from the page it stopped at.

    ``None`` is returned for an exhausted listing and for a faulted one alike, because the caller
    does the same thing with both; only the fault is counted.
    """
    try:
        return await asyncio.to_thread(_next_page, pages)
    except CategorizedError as exc:
        _count_root_fault(root, tally, "list", exc)
        return None


def _next_page(pages: Iterator[list[ObjectListing]]) -> list[ObjectListing] | None:
    """Advance ``pages`` one page, or return ``None`` at exhaustion — the blocking half of a page.

    Named rather than a ``next(pages, None)`` at the ``to_thread`` call site so the sentinel's type
    is declared once; the round trip lives here, which is why it is the part offloaded.
    """
    return next(pages, None)


async def _reclaim_page(
    conn: AsyncConnection,
    store: UploadOrphanStore,
    page: list[ObjectListing],
    grace: timedelta,
    *,
    tally: _Tally,
    sweep: _RootSweep,
) -> bool:
    """Classify one listing page and reclaim what it says is safe, in **page** order.

    Page order is store order and the pages themselves arrive in store order, so acting page by page
    deletes in exactly the sequence the flattened listing would have (ADR-0498 §2). The order is
    taken from the page rather than from the classify's rows for the reason ADR-0455 §3 gives: the
    planner is free to reorder an anti-join's output, the store is not, and a budget-truncated or
    fault-truncated root is only reproducible if the prefix it got through is deterministic.

    Only the database's own error class is caught for the classify — a bug in this module should
    still abort the pass — and ``asyncio.CancelledError`` is not a ``psycopg.Error``, so shutdown
    still ends the pass through the count-logging path. The classify is the fault ADR-0455 §5 called
    root-correlated because `local/runs/`'s whole listing went in as one array parameter; a
    page-wide parameter is what removes that correlation, and the skip-and-count stays because a
    ``statement_timeout`` or a dropped connection is not made impossible by a narrower statement.

    Returns:
        Whether this root should keep paging. ``False`` means the classify faulted and was counted.
    """
    candidates = [c for c in (_attribute(listing) for listing in page) if c is not None]
    sweep.listed += len(page)
    sweep.attributed += len(candidates)
    try:
        reclaimable = set(await reclaimable_upload_keys(conn, candidates, grace))
    except psycopg.Error as exc:
        _count_root_fault(sweep.root, tally, "classify", exc)
        return False
    for candidate in candidates:
        if sweep.budget_spent:
            break
        if candidate.key not in reclaimable:
            continue
        sweep.examined += 1
        try:
            tally.deleted += int(await _delete_if_still_reclaimable(conn, store, candidate, grace))
        except CategorizedError as exc:
            tally.failed += 1
            _log.warning(
                "reconciler: upload orphan sweep could not reclaim %s: %s", candidate.key, exc
            )
    return True


def _count_root_fault(root: str, tally: _Tally, step: str, exc: Exception) -> None:
    """Count a root-scoped fault so the pass still raises, and keep sweeping the sibling roots."""
    tally.failed += 1
    _log.warning(
        "reconciler: upload orphan sweep could not %s %s: %s; the remaining roots are still swept "
        "this pass, which still raises at the end",
        step,
        root,
        exc,
    )


async def _delete_if_still_reclaimable(
    conn: AsyncConnection,
    store: UploadOrphanStore,
    candidate: UploadOrphanCandidate,
    grace: timedelta,
) -> bool:
    """Re-decide one candidate against **current** store and database state, then delete it.

    Both halves are re-read, because both can change after the listing and only one of them is a
    row. The database fences catch a finalize or a re-mint that committed in the gap. The object's
    mtime is re-read from the store because the writers under this prefix are **object-before-row**
    — a vmcore's multi-GiB ``put_stream`` and a ``capture_traffic`` retry's re-PUT both land minutes
    before their ``artifacts`` row commits — so a key rewritten after the listing has no row to
    protect it yet, and re-checking the *listed* mtime would delete the bytes that were just
    written. That is the one fence that has to come from the store rather than from Postgres, and
    it is why this is not simply ``repair_leaked_images``' row re-check.

    Returns:
        Whether the object was deleted. ``False`` means the re-read declined it, or the object was
        already gone.
    """
    current = await _reread_from_store(store, candidate)
    if current is None:
        return False  # deleted by someone else between the listing and here
    if not await reclaimable_upload_keys(conn, [current.candidate], grace):
        return False  # a row landed, or the object was rewritten, after the classify
    await asyncio.to_thread(store.delete, current.candidate.key)
    _log.info(
        "reconciler: leaked upload object %s (etag %s) deleted (no artifacts row, no upload "
        "window, past grace)",
        current.candidate.key,
        current.etag,
    )
    return True


class _CurrentObject(NamedTuple):
    """One candidate as the store holds it *now*, read immediately before its delete.

    ``etag`` is the object's identity as observed in the very same round trip as ``candidate``'s
    refreshed ``last_modified``, so the two cannot disagree about which bytes were examined. The
    delete decision is made from this pair and the log line names it, which is what lets an
    operator reading a reclaim line tell which version of a re-PUT key went.
    """

    candidate: UploadOrphanCandidate
    etag: str


async def _reread_from_store(
    store: UploadOrphanStore, candidate: UploadOrphanCandidate
) -> _CurrentObject | None:
    """Stat ``candidate``'s key for its current mtime and etag, or ``None`` if it is gone.

    A single-object ``head``, not a LIST scoped to the key. Both are exact — a LIST on a full key
    plus an exact-match filter resolves to that key alone — but only one is a single round trip. A
    chunked window's parts are ``<base>.partNNNN`` (ADR-0104 §1) and a prefix listing paginates to
    exhaustion, so listing a leaked **base** key enumerated every one of its parts,
    1000 per round trip, to retrieve one ``LastModified``. That shape is not a corner case: a
    row-first reap failing partway through a chunked window leaves the base and all of its parts
    rowless together, which is precisely the candidate set this sweep exists to drain (#1575).
    """
    head = await asyncio.to_thread(store.head, candidate.key)
    if head is None:
        return None
    return _CurrentObject(replace(candidate, last_modified=head.last_modified), head.etag)


async def reclaimable_upload_keys(
    conn: AsyncConnection, candidates: list[UploadOrphanCandidate], grace: timedelta
) -> list[str]:
    """Return the subset of ``candidates`` that is safe to delete, deciding every fence in Postgres.

    A candidate is reclaimable only when no ``artifacts`` row references its key, its owner holds
    no ``upload_manifests`` row at all, and its store mtime is older than ``grace`` measured
    against Postgres ``now()``.

    This is deliberately usable for one key as well as for a listing page: it is the per-key
    re-check ADR-0453 §Consequences costed for its second residual (#1557), so wiring it into the
    reaper's ``_sweep_uncommitted_objects`` is a call rather than a rewrite.

    Args:
        conn: An async connection. One round trip is issued, whatever ``candidates``' length.
        candidates: The listed objects, each already attributed to an upload owner. The sweep passes
            one listing page (ADR-0498); the length is what bounds the array parameters' width.
        grace: How long past its store mtime an object is protected.

    Returns:
        The reclaimable keys, in no guaranteed order.
    """
    if not candidates:
        return []
    # Its own transaction: the connection is not autocommit, so without this the snapshot this
    # read opens would be held across every following blocking LIST and delete, pinning one of the
    # pool's ten slots idle-in-transaction for the length of an I/O-bound sweep.
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            _RECLAIMABLE_SQL,
            (
                [c.key for c in candidates],
                [c.last_modified for c in candidates],
                [c.owner_kind for c in candidates],
                [c.owner_id for c in candidates],
                grace,
            ),
        )
        return [row[0] for row in await cur.fetchall()]


def _warn_if_wholly_unattributable(root: str, listed: int, attributed: int) -> None:
    """Warn when a non-empty root yielded no attributable key — the key-layout drift signature.

    A zero return from this sweep is also its healthy steady state, so a sweep silently scoped out
    of the bucket it is meant to drain looks exactly like a clean one. The one condition that can
    cause that without raising is a key layout this parser no longer recognizes, and it is
    distinguishable: objects listed, none attributed.

    The counts are the root's totals over every page it got through, not one page's, and they are
    reported once after the root rather than per page (ADR-0498 §2). A per-page warning would fire
    for a page boundary that happened to isolate the unattributable keys, which is a listing
    artifact and not the drift this names.
    """
    if listed and not attributed:
        _log.warning(
            "reconciler: upload orphan sweep attributed none of %d object(s) under %s; the key "
            "layout may have drifted from %s, and the sweep is reclaiming nothing",
            listed,
            root,
            "<tenant>/<kind>/<uuid>/<name>",
        )


def _attribute(listing: ObjectListing) -> UploadOrphanCandidate | None:
    """Attribute one listed key to its upload owner, or ``None`` if it cannot be attributed.

    A key qualifies only as ``<tenant>/<kind>/<uuid>/<name>`` with a known upload owner kind, a
    parseable owner id, and a non-empty name. Anything else — a deeper path, a prefix marker, an
    owner id that is not a UUID — is dropped rather than deleted: without an owner there is no
    manifest row to fence on, and deleting it would be deleting on prefix membership alone.
    """
    parts = listing.key.split("/")
    if len(parts) != _KEY_COMPONENTS or not parts[3]:
        return None
    tenant, kind, owner_id = parts[0], parts[1], parts[2]
    if tenant != UPLOAD_TENANT or kind not in UPLOAD_OWNER_KINDS:
        return None
    try:
        parsed = UUID(owner_id)
    except ValueError:
        return None
    return UploadOrphanCandidate(
        key=listing.key,
        last_modified=listing.last_modified,
        owner_kind=kind,
        owner_id=parsed,
    )
