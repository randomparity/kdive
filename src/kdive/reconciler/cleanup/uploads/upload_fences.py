"""The row fences both upload deleters evaluate before destroying an object (ADR-0509).

Two passes delete objects under ``local/runs/`` and ``local/investigations/``: the reaper's
``_sweep_uncommitted_objects`` (ADR-0453) and the orphan sweep's ``_delete_if_still_reclaimable``
(ADR-0455, ADR-0502). They reach a key by different routes — one from a past-deadline manifest, the
other from a prefix listing — but they must agree on what a *protected* key is, because a key one
pass spares and the other destroys is protected by neither.

This module is that agreement, expressed once as :data:`_OWNER_FENCE_SQL` and embedded by both.
What each pass adds on top of it is its own: the orphan sweep adds a store-mtime grace, because its
candidate set is every rowless object under a root and it has no other reason to believe any of them
is dead; the reaper adds none, because its candidate set is one window it has just proved past its
deadline (ADR-0509 §3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.uploads.upload_manifest import UploadOwnerKind
from kdive.artifacts.uploads.write_lease import LIVE_HOLDER_SQL

#: The three reasons a key under an upload owner's prefix must not be deleted, correlated to a
#: candidate alias ``c`` exposing ``key``, ``owner_kind`` and ``owner_id``. Every one is a committed
#: row read in Postgres ``now()``, never a Python clock:
#:
#:   * an ``artifacts`` row reaches the key — the object is registered, and deleting it would strand
#:     the row;
#:   * the owner holds an ``upload_manifests`` row *at all* — not merely a live one. Upload keys are
#:     owner-addressed, so a re-minted window owns these very key names, and for the orphan sweep a
#:     lapsed window is the reaper's to collect rather than its own;
#:   * the owner holds a write lease whose holding job is still a live claim (ADR-0502). This is the
#:     fence for a writer that mints no upload window at all — local-libvirt's vmcore ``put_stream``
#:     is the reachable one. Liveness is the holding job's own
#:     (:data:`~kdive.artifacts.uploads.write_lease.LIVE_HOLDER_SQL`), read from that module so
#:     the passes that honour a lease and the pass that collects one cannot disagree.
#:
#: Written as the *negation* — the conditions under which a candidate survives — because that is
#: the form both callers need: the sweep selects the reclaimable subset of a page, and the reaper
#: selects whether its one key survived.
_OWNER_FENCE_SQL = f"""
      NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.object_key = c.key)
  AND NOT EXISTS (SELECT 1 FROM upload_manifests m
                  WHERE m.owner_kind = c.owner_kind AND m.owner_id = c.owner_id)
  AND NOT EXISTS (SELECT 1 FROM object_write_leases l
                  WHERE l.owner_kind = c.owner_kind AND l.owner_id = c.owner_id
                    AND {LIVE_HOLDER_SQL})
"""

# The orphan sweep's classify: the shared fences plus its store-mtime grace, over a listing page.
# Its four parallel arrays are a page wide, not a root wide (ADR-0498). The width is visible to the
# planner — ``unnest``'s row estimate tracks the array's real length — so the plan does change with
# it, and it changes toward the index: at a page's width the ``artifacts`` anti-join is a nested
# loop over #1570's ``object_key`` btree, where a root's width tipped it to a hash anti-join over a
# sequential scan. Measured wall-clock time is flat across the widths (ADR-0498 §5).
_RECLAIMABLE_SQL = f"""
SELECT c.key
FROM unnest(%s::text[], %s::timestamptz[], %s::text[], %s::uuid[])
     AS c(key, last_modified, owner_kind, owner_id)
WHERE c.last_modified < now() - %s
  AND {_OWNER_FENCE_SQL}
"""

# The reaper's re-check: the shared fences alone, over one key already attributed to its owner. The
# one-row candidate is built in the SELECT rather than passed through ``unnest`` so the fragment
# above sees the same ``c`` alias in both statements and neither can be edited without the other.
_KEY_SURVIVES_SQL = f"""
SELECT 1
FROM (SELECT %s::text AS key, %s::text AS owner_kind, %s::uuid AS owner_id) AS c
WHERE {_OWNER_FENCE_SQL}
"""


@dataclass(frozen=True, slots=True)
class UploadOrphanCandidate:
    """One listed object attributed to the upload owner whose prefix it sits under."""

    key: str
    last_modified: datetime
    owner_kind: UploadOwnerKind
    owner_id: UUID


async def reclaimable_upload_keys(
    conn: AsyncConnection, candidates: list[UploadOrphanCandidate], grace: timedelta
) -> list[str]:
    """Return the subset of ``candidates`` that is safe to delete, deciding every fence in Postgres.

    A candidate is reclaimable only when it passes every fence in :data:`_OWNER_FENCE_SQL` and its
    store mtime is older than ``grace`` measured against Postgres ``now()``.

    Deliberately usable for one key as well as for a listing page: the orphan sweep calls it once
    per page to classify and again per key immediately before deleting.

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


async def owner_key_is_fenced(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID, key: str
) -> bool:
    """Report whether a fence now protects ``key`` — the reaper's re-check (ADR-0509 §1).

    Opens **no** transaction of its own, unlike :func:`reclaimable_upload_keys`. The caller is
    ``_sweep_uncommitted_objects``, which runs this inside the transaction already holding the
    owner's advisory lock; opening one here would be a savepoint whose snapshot is the caller's
    anyway, and the point of the call is to read state as of *that* transaction.

    Carries no store-mtime term. The reaper's candidates are one window's objects, past deadline and
    with the window's row already deleted under this same lock, so the grace the orphan sweep needs
    over an undifferentiated root would only defer the reap it exists to perform (ADR-0509 §3).

    Args:
        conn: An async connection, inside the caller's locked transaction.
        owner_kind: The upload owner kind whose prefix ``key`` sits under.
        owner_id: The owner id.
        key: The object key about to be deleted.

    Returns:
        ``True`` when an ``artifacts`` row, an ``upload_manifests`` row or a live write lease now
        protects ``key`` — the writers the reaper's phase 1 could not have seen.
    """
    async with conn.cursor() as cur:
        await cur.execute(_KEY_SURVIVES_SQL, (key, owner_kind, owner_id))
        return await cur.fetchone() is None
