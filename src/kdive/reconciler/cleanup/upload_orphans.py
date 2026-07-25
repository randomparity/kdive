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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.storage import ObjectListing
from kdive.artifacts.upload_manifest import UPLOAD_TENANT
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

#: The default orphan grace, protecting an unreferenced object for this long past its store mtime
#: **in addition to** the configured upload-window TTL (see :func:`repair_leaked_upload_objects`).
DEFAULT_UPLOAD_ORPHAN_GRACE = timedelta(hours=24)

# The number of ``/``-separated components in an upload object key: ``<tenant>/<kind>/<id>/<name>``.
# ``validate_key_component`` rejects ``/`` in every component, so a well-formed key has exactly
# this many and a key with any other shape was not written by the upload lane.
_KEY_COMPONENTS = 4

# One statement decides reclaimability, and it serves both the bulk classify and the per-key
# re-check so the two cannot drift into disagreeing about what is safe to delete (ADR-0455 §2).
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
    """The reaper's port plus the mtime-bearing listing the orphan sweep needs."""

    def list_prefix_with_mtime(self, prefix: str) -> list[ObjectListing]: ...


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

    Walks ``local/runs/`` and ``local/investigations/``, attributes each listed key to its owner by
    parsing it, classifies the whole listing in one query, then re-runs that same predicate for
    each reclaimable key immediately before deleting it — so a finalize or a re-mint that commits
    between the listing and the delete protects its object.

    The reclaim threshold is ``orphan_grace + upload_ttl``, and the second term is not padding
    (ADR-0455 §2). The manifest fence protects an object only until the reaper deletes its window's
    row, which happens a TTL after the mint — so a threshold measured on the object's mtime alone
    and merely *equal* to the TTL makes the bytes reclaimable within seconds of the reap, and one
    above it makes them reclaimable in the very pass that reaped them. Summing the two puts the
    threshold a full ``orphan_grace`` past the earliest reap of any window the object could have
    belonged to, whatever the operator sets ``KDIVE_UPLOAD_TTL_SECONDS`` to.

    Nothing is caught. Unlike the reaper's phase 2 — which tolerates a failed key because its row
    delete has already committed and there is nothing left to retry — this sweep commits nothing,
    so a store fault costs one pass, re-derives the identical candidates on the next, and reaches
    the ADR-0190 group-E error counter via ``_run_repair_plan``'s ``failures`` (ADR-0455 §4).

    Args:
        conn: An async connection. Each query runs in its own short transaction so no snapshot is
            held across the blocking store calls.
        store: The object store to list and delete through.
        orphan_grace: How long past the earliest possible reap an object is protected.
        upload_ttl: The configured upload-window TTL, added to ``orphan_grace``.

    Returns:
        The number of objects deleted; one INFO line per delete.

    Raises:
        CategorizedError: the store failed to list a root or delete an object
            (:attr:`~kdive.domain.errors.ErrorCategory.INFRASTRUCTURE_FAILURE`).
    """
    grace = orphan_grace + upload_ttl
    deleted = 0
    for root in UPLOAD_ORPHAN_ROOTS:
        listings = await asyncio.to_thread(store.list_prefix_with_mtime, root)
        candidates = [c for c in (_attribute(listing) for listing in listings) if c is not None]
        _warn_if_wholly_unattributable(root, len(listings), len(candidates))
        reclaimable = set(await reclaimable_upload_keys(conn, candidates, grace))
        # Deleting in listing order rather than in the classify query's row order keeps a partial
        # pass reproducible: the planner is free to reorder an anti-join's output, the store is not.
        for candidate in candidates:
            if candidate.key not in reclaimable:
                continue
            if not await reclaimable_upload_keys(conn, [candidate], grace):
                continue  # a row landed between the classify and the delete
            await asyncio.to_thread(store.delete, candidate.key)
            _log.info(
                "reconciler: leaked upload object %s deleted (no artifacts row, no upload "
                "window, past grace)",
                candidate.key,
            )
            deleted += 1
    return deleted


async def reclaimable_upload_keys(
    conn: AsyncConnection, candidates: list[UploadOrphanCandidate], grace: timedelta
) -> list[str]:
    """Return the subset of ``candidates`` that is safe to delete, deciding every fence in Postgres.

    A candidate is reclaimable only when no ``artifacts`` row references its key, its owner holds
    no ``upload_manifests`` row at all, and its store mtime is older than ``grace`` measured
    against Postgres ``now()``.

    This is deliberately usable for one key as well as for a whole listing: it is the per-key
    re-check ADR-0453 §Consequences costed for its second residual (#1557), so wiring it into the
    reaper's ``_sweep_uncommitted_objects`` is a call rather than a rewrite.

    Args:
        conn: An async connection. One round trip is issued, whatever ``candidates``' length.
        candidates: The listed objects, each already attributed to an upload owner.
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
