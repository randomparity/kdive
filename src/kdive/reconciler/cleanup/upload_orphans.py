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
from kdive.reconciler.cleanup.uploads import UPLOAD_OWNER_KINDS, UploadStore

_log = logging.getLogger(__name__)

# The tenant every upload window mints under (``mcp.tools.catalog.artifacts.uploads``). Other
# tenants (``remote-libvirt``, ``fault-inject``) never hold an upload window, so they are out of
# scope; see ADR-0455 §Consequences.
_TENANT = "local"

#: The object-store roots this sweep walks — one per upload owner kind, derived from the reaper's
#: own owner-kind table so the sweep's scope cannot drift from what the reaper reaps.
UPLOAD_ORPHAN_ROOTS: tuple[str, ...] = tuple(f"{_TENANT}/{kind}/" for kind in UPLOAD_OWNER_KINDS)

#: How long an unreferenced object under an upload root is protected from reclaim, measured from
#: its store mtime. Required for correctness, not polish: a presigned PUT may begin before the
#: window's deadline and complete after it, so deleting on prefix membership alone would destroy a
#: live upload's bytes (ADR-0455 §2). Sized far above every legitimate rowless interval under these
#: roots — a ``capture_traffic`` pcap's PUT and its row share one transaction, a vmcore object is
#: PUT minutes before ``finalize_capture`` inserts its rows — because an extra day of leak is a
#: cost bug and a deleted live object is a correctness bug.
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
    conn: AsyncConnection, store: UploadOrphanStore, grace: timedelta
) -> int:
    """Delete objects under the upload roots that no ``artifacts`` or manifest row can reach.

    Walks ``local/runs/`` and ``local/investigations/``, attributes each listed key to its owner by
    parsing it, classifies the whole listing in one query, then re-runs that same predicate for
    each reclaimable key immediately before deleting it — so a finalize or a re-mint that commits
    between the listing and the delete protects its object.

    Nothing is caught. Unlike the reaper's phase 2 — which tolerates a failed key because its row
    delete has already committed and there is nothing left to retry — this sweep commits nothing,
    so a store fault costs one pass, re-derives the identical candidates on the next, and reaches
    the ADR-0190 group-E error counter via ``_run_repair_plan``'s ``failures`` (ADR-0455 §4).

    Returns:
        The number of objects deleted; one INFO line per delete.

    Raises:
        CategorizedError: the store failed to list a root or delete an object
            (:attr:`~kdive.domain.errors.ErrorCategory.INFRASTRUCTURE_FAILURE`).
    """
    deleted = 0
    for root in UPLOAD_ORPHAN_ROOTS:
        listings = await asyncio.to_thread(store.list_prefix_with_mtime, root)
        candidates = [c for c in (_attribute(listing) for listing in listings) if c is not None]
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
    async with conn.cursor() as cur:
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
    if tenant != _TENANT or kind not in UPLOAD_OWNER_KINDS:
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
