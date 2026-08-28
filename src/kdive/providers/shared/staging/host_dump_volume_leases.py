"""The durable claim a capture holds over its System's host_dump volume (ADR-0562, #1955).

ADR-0094 requires the orphaned-volume sweep never to delete a volume a live capture owns, and
ADR-0557 made its live-holder predicate effective while disclosing what a predicate cannot be. A
``queued`` capture job is deliberately not a live holder — an unserved lane has no transition that
forces it terminal, so treating it as one would pin a known orphan forever — which leaves the sweep
sampling ``jobs.state`` at one instant and deleting at another. A worker that claims the job in
between makes the sample stale, and ``host_dump_capture``'s delete-stale-then-dump pair puts a
**new** volume at the same deterministic name for the sweep's name-addressed delete to resolve.

A row here is the declaration that closes it, and the fence is only half of the mechanism. The other
half is the **lock**: :func:`hold_host_dump_volume_lease` mints under ``(SYSTEM, system_id)``, and
the sweep takes the same lock before its final classification and holds it across the delete. A mint
therefore either precedes the classification — which then sees the row — or blocks until the delete
has already happened, in which case the capture's own stale-volume delete finds nothing and its dump
creates a volume the sweep has already passed. Mint-before-touch and classify-before-delete become
totally ordered, which is what makes this a closure rather than a fourth mitigation.

The lease carries **no deadline of its own**. Its liveness is its holding job's — the lease the
worker heartbeats — so a capture keeps its fence however long it dumps and downloads, and a lease
whose holder died fences nothing from the next pass onward. That predicate is
:data:`~kdive.artifacts.uploads.write_lease.LIVE_HOLDER_SQL`, imported rather than restated for
the reason ADR-0522 gives: one hand-written copy of "the holder is still alive" could drift from
the one runner enforces, and it would drift silently.

Placed under ``providers.shared`` beside
:mod:`~kdive.providers.shared.staging.rootfs_fetch_leases` for that module's reason: the writer
is a job handler and the reader is the reconciler, so neither owns
it, and what it describes is provider state — a libvirt storage volume — not an artifact.
"""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.uploads.write_lease import LIVE_HOLDER_SQL
from kdive.db.locks import LockScope, advisory_xact_lock, require_top_level_transaction

_log = logging.getLogger(__name__)

#: The fence probe: does this System have a lease whose holder is still a live claim? ``EXISTS``
#: rather than a count, because the sweep needs one bit and the primary key makes it a range scan
#: that stops at the first live row. Both halves are evaluated by Postgres, so no worker clock
#: enters the comparison — this tree's ``now()`` is session-TZ rather than UTC.
_LIVE_LEASE_SQL = f"""SELECT 1 FROM host_dump_volume_leases l
                      WHERE l.system_id = %s AND {LIVE_HOLDER_SQL}
                      LIMIT 1"""  # noqa: S608 - LIVE_HOLDER_SQL is a module constant


async def hold_host_dump_volume_lease(conn: AsyncConnection, system_id: UUID, job_id: UUID) -> None:
    """Commit an idempotent dump-volume lease before provider work begins.

    The connection must be outside a transaction so the sweep can observe the lease. The
    shared System advisory lock orders the insert against sweep classification (ADR-0562).
    """
    require_top_level_transaction(conn, "hold_host_dump_volume_lease")
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        await conn.execute(
            "INSERT INTO host_dump_volume_leases (system_id, job_id) VALUES (%s, %s) "
            "ON CONFLICT (system_id, job_id) DO NOTHING",
            (system_id, job_id),
        )


async def release_host_dump_volume_lease(
    conn: AsyncConnection, system_id: UUID, job_id: UUID
) -> None:
    """Drop ``job_id``'s claim on this System's dump volume — its operation is over.

    Takes no lock and opens no transaction: the caller supplies both. The intended caller is
    ``finalize_capture``, which runs this inside the transaction committing the ``artifacts`` rows,
    by which point the provider has already removed the volume in its own ``finally``. No advisory
    lock is needed because the row's disappearance can only ever *permit* a reap, never mislead one.

    Scoped to this holder's row, so it cannot release a lease it does not hold.
    """
    await conn.execute(
        "DELETE FROM host_dump_volume_leases WHERE system_id = %s AND job_id = %s",
        (system_id, job_id),
    )


async def has_live_host_dump_volume_lease(conn: AsyncConnection, system_id: UUID) -> bool:
    """Whether a capture with a live holder currently claims ``system_id``'s dump volume.

    Read by the sweep inside the transaction that holds ``(SYSTEM, system_id)``, which is what makes
    the answer usable: outside that lock it would be another sample of the kind ADR-0557 disclosed.
    """
    async with conn.cursor() as cur:
        await cur.execute(_LIVE_LEASE_SQL, (system_id,))
        return await cur.fetchone() is not None


async def reap_stale_host_dump_volume_leases(conn: AsyncConnection) -> int:
    """Delete every dump-volume lease whose holding job is no longer a live claim.

    Hygiene, not exposure. Because the sweep's fence carries :data:`LIVE_HOLDER_SQL` itself, a lease
    this pass has not yet collected already protects nothing — so running late here costs no
    correctness. What it bounds is table growth, and that growth is guaranteed rather than
    exceptional: ``capture_handler``'s ``except Exception:`` does not release the lease
    (deliberately — a worker killed mid-capture releases nothing at all), so every failed capture
    leaves a row behind.

    ``ON DELETE CASCADE`` already collects a lease whose ``jobs`` or ``systems`` row is gone; this
    collects the far commoner case of a job that merely stopped running.

    Returns:
        The number of leases deleted.
    """
    async with conn.transaction(), conn.cursor() as cur:
        # noqa justified: LIVE_HOLDER_SQL is a module constant, never caller input.
        await cur.execute(f"DELETE FROM host_dump_volume_leases l WHERE NOT {LIVE_HOLDER_SQL}")  # noqa: S608
        reaped = cur.rowcount
    if reaped:
        _log.info(
            "reconciler: reaped %d host_dump volume lease(s) whose holding job is no longer live",
            reaped,
        )
    return reaped
