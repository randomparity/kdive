"""Write leases over an upload owner's object prefix (ADR-0502, #1687).

A job that writes objects under ``<tenant>/<owner_kind>/<owner_id>/`` without minting an upload
window has, until this module, nothing the ADR-0455 orphan sweep can see: the sweep decides a key
reclaimable from committed ``artifacts`` / ``upload_manifests`` rows plus a store HEAD, and then
deletes holding nothing. A write landing after that decision and before the delete is destroyed —
ADR-0497 §3's disclosed residual, which its finalize-side verify turns into a failed job rather than
preventing.

A lease is that missing declaration, and the fence is only half of the mechanism. The other half is
the **lock**: :func:`hold_write_lease` mints under the owner's advisory lock, and the sweep takes
the same lock before it deletes, so a mint either precedes the sweep's classify (which then sees
it) or blocks until the delete is already done (so the write follows the delete). Mint-before-write
and classify-before-delete become totally ordered, which is what makes this a closure rather than a
fourth mitigation.

The lease carries **no deadline of its own**. Its liveness is its holding job's — the lease the
worker heartbeats — so a capture keeps its fence however long it streams, and a lease whose holder
died fences nothing from the next pass onward. A second independent deadline would have to exceed
the longest capture, and nothing in the tree bounds one.
"""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.uploads.upload_manifest import UploadOwnerKind, lock_scope_for
from kdive.db.locks import advisory_xact_lock, require_top_level_transaction

_log = logging.getLogger(__name__)

#: The liveness predicate a write lease is only a fence while it satisfies, correlated to a
#: ``object_write_leases`` alias ``l``. Shared verbatim between the orphan sweep's classify and
#: :func:`reap_stale_write_leases` so the pass that *honours* a lease and the pass that *collects*
#: one cannot disagree about which leases are live — a reap looser than the fence would delete a
#: row that is actively protecting bytes.
#:
#: ``state = 'running' AND lease_expires_at > now()`` is ``jobs``' own definition of a claimed,
#: un-lapsed job: ``dequeue`` reclaims exactly its complement, and ``heartbeat`` advances
#: ``lease_expires_at`` for as long as the handler runs. Evaluated in Postgres ``now()``, never a
#: Python clock.
LIVE_HOLDER_SQL = """EXISTS (SELECT 1 FROM jobs j
                            WHERE j.id = l.job_id
                              AND j.state = 'running'
                              AND j.lease_expires_at > now())"""


async def hold_write_lease(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID, job_id: UUID
) -> None:
    """Declare that ``job_id`` is about to write objects under ``owner_kind``/``owner_id``.

    Must be called **before** the first write and committed, because the sweep's fence reads
    committed rows. It commits its own transaction for that reason: an uncommitted lease protects
    nothing, so a caller cannot usefully batch this into a later transaction — and it *checks* that
    it can, via :func:`~kdive.db.locks.require_top_level_transaction`. On a connection already in a
    transaction this whole block would degrade to a savepoint: the lease would stay invisible for
    the entire write it exists to fence, and the owner lock would be held until the caller's
    commit, which is precisely the multi-GiB lock hold ADR-0244 forbids. Both failures are silent
    at the call site, so the precondition is enforced rather than documented.

    The owner's advisory lock — the one the sweep's per-key delete also takes — is held over the
    insert. That is not for row-level safety (the upsert is atomic on its own) but for the ordering
    ADR-0502 rests on: while the sweep holds the lock, no mint can slip between its classify and its
    delete. The critical section is one INSERT, so this does not reintroduce the multi-GiB
    lock-holding ADR-0244 exists to avoid — the *write* runs after this returns, holding nothing.

    Idempotent per holder. The primary key includes ``job_id``, so a retried attempt of the same job
    is a no-op and two concurrent captures of one Run hold two leases, neither able to release the
    other's.

    Args:
        conn: An async connection with **no** transaction open; one is opened here.
        owner_kind: The upload owner kind whose prefix is being written.
        owner_id: The owner id.
        job_id: The holding job. Its ``jobs`` row is what the fence tests for liveness, so a lease
            outlives its usefulness exactly when the job stops being a live claim.
    """
    require_top_level_transaction(conn, "hold_write_lease")
    async with conn.transaction(), advisory_xact_lock(conn, lock_scope_for(owner_kind), owner_id):
        await conn.execute(
            "INSERT INTO object_write_leases (owner_kind, owner_id, job_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (owner_kind, owner_id, job_id) DO NOTHING",
            (owner_kind, owner_id, job_id),
        )


async def release_write_lease(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID, job_id: UUID
) -> None:
    """Drop ``job_id``'s lease on this owner's prefix — its writes are registered or abandoned.

    Takes no lock and opens no transaction: the caller supplies both. The intended caller is
    ``finalize_capture``, which runs this inside the transaction that commits the ``artifacts`` rows
    and already holds ``LockScope.RUN``, so the lease disappears in the same commit that makes the
    objects row-protected and there is no instant at which neither fence applies.

    Scoped to this holder's row, so releasing one of two concurrent captures' leases leaves the
    other's standing.
    """
    await conn.execute(
        "DELETE FROM object_write_leases WHERE owner_kind = %s AND owner_id = %s AND job_id = %s",
        (owner_kind, owner_id, job_id),
    )


async def reap_stale_write_leases(conn: AsyncConnection) -> int:
    """Delete every write lease whose holding job is no longer a live claim.

    Hygiene, not exposure. Because the sweep's fence carries :data:`LIVE_HOLDER_SQL` itself, a lease
    this pass has not yet collected already protects nothing — so unlike the upload-window reaper
    (ADR-0444), running late here costs no correctness. What it bounds is table growth, and that
    growth is guaranteed rather than exceptional: ``capture_handler``'s ``except Exception:`` does
    not release the lease (deliberately — a worker killed mid-write releases nothing at all), so
    every failed capture leaves a row behind.

    ``ON DELETE CASCADE`` on ``job_id`` already collects a lease whose ``jobs`` row is deleted; this
    collects the far commoner case of a job that merely stopped running.

    Returns:
        The number of leases deleted.
    """
    async with conn.transaction(), conn.cursor() as cur:
        # noqa justified: LIVE_HOLDER_SQL is a module constant, never caller input.
        await cur.execute(f"DELETE FROM object_write_leases l WHERE NOT {LIVE_HOLDER_SQL}")  # noqa: S608
        reaped = cur.rowcount
    if reaped:
        _log.info(
            "reconciler: reaped %d write lease(s) whose holding job is no longer live", reaped
        )
    return reaped
