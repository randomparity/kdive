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
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg import AsyncConnection

from kdive.artifacts.storage import ObjectVersion, VersionBatch, VersionPage
from kdive.artifacts.uploads.upload_manifest import (
    UPLOAD_OWNER_KINDS,
    UPLOAD_TENANT,
    lock_scope_for,
)
from kdive.db.locks import require_top_level_transaction, try_advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.uploads.upload_fences import (
    UploadOrphanCandidate,
    reclaimable_upload_keys,
)

_log = logging.getLogger(__name__)

#: The object-store roots this sweep walks — one per upload owner kind, and both halves of the
#: prefix derived rather than written out here: the tenant from the constant the mint sites share
#: and the kinds from the reaper's own owner-kind table. A sweep whose prefix drifts from the
#: mint's lists nothing and reports a healthy zero forever while the leak resumes, so neither half
#: is a literal. Other tenants (``remote-libvirt``, ``fault-inject``) never hold an upload window.
UPLOAD_ORPHAN_ROOTS: tuple[str, ...] = tuple(
    f"{UPLOAD_TENANT}/{kind}/" for kind in UPLOAD_OWNER_KINDS
)

#: Maximum inventory/deletion work charged per root and pass. Successful captures charge their
#: immutable target count, a denied capture charges its requested allowance, and an empty capture
#: race charges the identity broad inventory observed. The reconciler runs repairs serially, so the
#: cap keeps a historical backlog or repeated denial from delaying unrelated repair groups. A
#: later pass restarts at the root and rediscovers every survivor.
MAX_RECLAIMS_PER_ROOT = 200

#: Maximum immutable versions or markers one key may charge in a root pass. Reaching the cap
#: resumes the broad listing after the whole key, so one hot or denied history cannot starve a
#: sibling. The skipped history is rediscovered when the next pass restarts at the root.
MAX_VERSIONS_PER_KEY = 20

# The number of ``/``-separated components in an upload object key: ``<tenant>/<kind>/<id>/<name>``.
# ``validate_key_component`` rejects ``/`` in every component, so a well-formed key has exactly
# this many and a key with any other shape was not written by the upload lane.
_KEY_COMPONENTS = 4


class UploadOrphanStore(Protocol):
    """The public version-inventory and exact-deletion surface the orphan sweep consumes."""

    def list_version_page(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
        max_keys: int = 1000,
    ) -> VersionPage: ...

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch: ...
    def delete_batch(self, batch: VersionBatch) -> bool: ...


async def repair_leaked_upload_objects(
    conn: AsyncConnection,
    store: UploadOrphanStore,
    orphan_grace: timedelta,
    upload_ttl: timedelta,
) -> int:
    """Delete unreachable upload versions after their database fences commit.

    The grace period includes the upload-window TTL so objects remain protected after their
    manifest expires. Store operations run outside owner locks; failures are accumulated across
    roots and raised after the pass. Returns only deletions confirmed by completed batches.
    """
    grace = orphan_grace + upload_ttl
    tally = _Tally()
    try:
        for root in UPLOAD_ORPHAN_ROOTS:
            await _sweep_root(conn, store, root, grace, tally)
    except BaseException:  # noqa: BLE001 - logged and re-raised, never swallowed
        # Any abort — a listing fault, a dropped database connection, or cancellation
        # at shutdown — can arrive after this pass has already deleted irreversibly, and
        # ``_run_repair_plan`` records no count for a repair that raises. Put the counts on the
        # record before the exception carries them off (ADR-0455 §5).
        tally.log_abort()
        raise
    return tally.reported()


@dataclass
class _Tally:
    """Completed-batch confirmations and failed operations across roots reached by one pass.

    ``deleted`` excludes any nonlatest identities a batch removed before raising. The narrow store
    API reports only completion or failure, so claiming a partial count would invent knowledge the
    caller does not have; version inventory remains the durable record of survivors.
    """

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
            "reconciler: upload orphan sweep aborted before its last root; it had confirmed %d "
            "version target(s) reclaimed by completed batches and counted %d failed operation(s). "
            "A failed batch may have made uncounted partial progress. Neither count reaches the "
            "repairs counter because this pass raises, and a root it had not reached was not "
            "swept.",
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
            "reconciler: upload orphan sweep confirmed %d version target(s) reclaimed by completed "
            "batches and encountered %d failed operation(s); a failed batch may have made "
            "uncounted partial progress. The confirmed count is not reported to the repairs "
            "counter because this pass raises",
            self.deleted,
            self.failed,
        )

    def reported(self) -> int:
        """Return the reclaimed count, or log it and raise if any key failed."""
        if not self.failed:
            return self.deleted
        self.log()
        raise CategorizedError(
            f"upload orphan sweep encountered {self.failed} failed operation(s); {self.deleted} "
            "were confirmed reclaimed by completed batches this pass. A failed batch may have "
            "made uncounted partial progress. Every survivor remains discoverable in version "
            "inventory for the next pass, but a key that fails every pass (an object-lock hold, a "
            "per-key deny) leaks until it is cleared.",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )


@dataclass
class _RootSweep:
    """One root's version budget, page-boundary de-duplication, and drift counts."""

    root: str
    charged: int = 0
    listed: int = 0
    attributed: int = 0
    seen_keys: set[str] = field(default_factory=set)

    @property
    def budget_spent(self) -> bool:
        """Whether this root charged its 200-version allowance."""
        return self.charged >= MAX_RECLAIMS_PER_ROOT

    @property
    def remaining(self) -> int:
        """Inventory/deletion work units left in this root's pass allowance."""
        return MAX_RECLAIMS_PER_ROOT - self.charged


async def _sweep_root(
    conn: AsyncConnection,
    store: UploadOrphanStore,
    root: str,
    grace: timedelta,
    tally: _Tally,
) -> None:
    """Reclaim one root through public version pages with 200/root and 20/key bounds."""
    sweep = _RootSweep(root)
    key_marker: str | None = None
    version_marker: str | None = None
    while not sweep.budget_spent:
        page = await _next_page_or_fault(
            store,
            root,
            tally,
            key_marker=key_marker,
            version_id_marker=version_marker,
        )
        if page is None:
            break
        resume_after = await _reclaim_page(conn, store, page, grace, tally=tally, sweep=sweep)
        if sweep.budget_spent:
            break
        if resume_after is not None:
            key_marker = resume_after
            version_marker = None
            continue
        if not page.is_truncated:
            break
        key_marker = page.next_key_marker
        version_marker = page.next_version_id_marker
    if sweep.budget_spent:
        _log.info(
            "reconciler: upload orphan sweep stopped at its %d-version budget for %s; any "
            "remaining history is reclaimed by following passes",
            MAX_RECLAIMS_PER_ROOT,
            root,
        )
    _warn_if_wholly_unattributable(root, sweep.listed, sweep.attributed)


async def _next_page_or_fault(
    store: UploadOrphanStore,
    root: str,
    tally: _Tally,
    *,
    key_marker: str | None,
    version_id_marker: str | None,
) -> VersionPage | None:
    """Fetch one bounded public version page; a fault ends only this root."""
    try:
        return await asyncio.to_thread(
            store.list_version_page,
            root,
            key_marker=key_marker,
            version_id_marker=version_id_marker,
            max_keys=1000,
        )
    except CategorizedError as exc:
        _count_root_fault(root, tally, "list", exc)
        return None


async def _reclaim_page(
    conn: AsyncConnection,
    store: UploadOrphanStore,
    page: VersionPage,
    grace: timedelta,
    *,
    tally: _Tally,
    sweep: _RootSweep,
) -> str | None:
    """Capture and fence each new key in page order; return a capped key-only resume marker."""
    for entry in page.entries:
        sweep.listed += 1
        attributed = _attribute(entry)
        if attributed is None:
            continue
        sweep.attributed += 1
        if attributed.key in sweep.seen_keys:
            continue
        sweep.seen_keys.add(attributed.key)
        if sweep.budget_spent:
            break
        limit = min(MAX_VERSIONS_PER_KEY, sweep.remaining)
        try:
            batch = await asyncio.to_thread(store.capture_exact_versions, attributed.key, limit)
        except CategorizedError as exc:
            sweep.charged += limit
            tally.failed += 1
            _log.warning(
                "reconciler: upload orphan sweep could not capture %s: %s", attributed.key, exc
            )
            return attributed.key
        if not batch.targets:
            # Broad inventory observed at least one identity for this key. Charge that observed
            # work even when a concurrent exact delete makes the capture empty, or repeated races
            # could evade the root brake indefinitely.
            sweep.charged += 1
            return attributed.key
        sweep.charged += len(batch.targets)
        candidate = UploadOrphanCandidate(
            key=attributed.key,
            last_modified=max(target.last_modified for target in batch.targets),
            owner_kind=attributed.owner_kind,
            owner_id=attributed.owner_id,
        )
        try:
            tally.deleted += await _delete_if_still_reclaimable(
                conn, store, candidate, batch, grace
            )
        except (CategorizedError, psycopg.Error) as exc:
            tally.failed += 1
            _log.warning(
                "reconciler: upload orphan sweep could not reclaim %s: %s", candidate.key, exc
            )
            return batch.key
        if not batch.history_complete:
            return batch.key
    return None


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
    batch: VersionBatch,
    grace: timedelta,
) -> int:
    """Fence, unlock, and return only identities confirmed by a completed delete batch.

    A raised batch may already have deleted nonlatest targets. It propagates without per-identity
    logs because this narrow API cannot report which prefix of those deletes completed.
    """
    # A savepoint here would hold the owner lock for the rest of the pass instead of for this one
    # key, so the transaction has to be a real one. ``_run_repair_plan`` hands each repair a
    # freshly pooled connection and every prior per-key transaction commits, so this holds
    # today; it is asserted because nothing at this call site would show if it stopped.
    require_top_level_transaction(conn, "the upload orphan sweep's per-key delete")
    async with conn.transaction():
        if not await try_advisory_xact_lock(
            conn, lock_scope_for(candidate.owner_kind), candidate.owner_id
        ):
            _log.info(
                "reconciler: upload orphan sweep left %s for a later pass; owner %s/%s is locked, "
                "so a writer or a reap is active on it (ADR-0502)",
                candidate.key,
                candidate.owner_kind,
                candidate.owner_id,
            )
            return 0
        if not await reclaimable_upload_keys(conn, [candidate], grace):
            return 0
    complete = await asyncio.to_thread(store.delete_batch, batch)
    deleted = tuple(target for target in batch.targets if complete or not target.is_latest)
    for target in deleted:
        kind = "delete marker" if target.is_delete_marker else "data version"
        _log.info(
            "reconciler: leaked upload object %s version %s (%s) deleted (no artifacts row, no "
            "upload window, no live write lease, past grace)",
            target.key,
            target.version_id,
            kind,
        )
    return len(deleted)


def _warn_if_wholly_unattributable(root: str, listed: int, attributed: int) -> None:
    """Warn when a non-empty root yielded no attributable key — the key-layout drift signature.

    A zero return from this sweep is also its healthy steady state, so a sweep silently scoped out
    of the bucket it is meant to drain looks exactly like a clean one. The one condition that can
    cause that without raising is a key layout this parser no longer recognizes, and it is
    distinguishable: version entries listed, none attributed.

    The counts are the root's totals over every page it got through, not one page's, and they are
    reported once after the root rather than per page (ADR-0498 §2). A per-page warning would fire
    for a page boundary that happened to isolate the unattributable keys, which is a listing
    artifact and not the drift this names.
    """
    if listed and not attributed:
        _log.warning(
            "reconciler: upload orphan sweep attributed none of %d version entry(s) under %s; the "
            "key "
            "layout may have drifted from %s, and the sweep is reclaiming nothing",
            listed,
            root,
            "<tenant>/<kind>/<uuid>/<name>",
        )


def _attribute(listing: ObjectVersion) -> UploadOrphanCandidate | None:
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
    # Matched out of the curated tuple rather than merely tested against it, so the attributed kind
    # carries its ``UploadOwnerKind`` type onward: ADR-0502's per-key delete feeds it to
    # ``lock_scope_for``, which must never be reached with a kind nothing locks under.
    owner_kind = next((known for known in UPLOAD_OWNER_KINDS if known == kind), None)
    if tenant != UPLOAD_TENANT or owner_kind is None:
        return None
    try:
        parsed = UUID(owner_id)
    except ValueError:
        return None
    return UploadOrphanCandidate(
        key=listing.key,
        last_modified=listing.last_modified,
        owner_kind=owner_kind,
        owner_id=parsed,
    )
