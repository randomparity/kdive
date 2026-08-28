"""Durable live-holder leases that pin uploaded rootfs bases during staging (ADR-0515).

Fetchers use a synchronous autocommit connection; reclaimers query the same holder-liveness fence
asynchronously. A lease pins a base only while its owning job remains a live claim.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

import psycopg
from psycopg import AsyncConnection

from kdive.artifacts.uploads.write_lease import LIVE_HOLDER_SQL

_log = logging.getLogger(__name__)

_ACQUIRE_SQL = (
    "INSERT INTO rootfs_fetch_leases (id, investigation_id, token, system_id, job_id) "
    "VALUES (%s, %s, %s, %s, %s)"
)
_RELEASE_SQL = "DELETE FROM rootfs_fetch_leases WHERE id = %s"

#: The pin probe. ``EXISTS`` rather than a count: the gate needs one bit and the index makes this a
#: range scan that stops at the first live row.
#:
#: The liveness half is :data:`~kdive.artifacts.uploads.write_lease.LIVE_HOLDER_SQL`, imported
#: rather than restated (ADR-0522). It is ``jobs``' own definition of a claimed, un-lapsed job —
#: ``state = 'running' AND lease_expires_at > now()``, which ``dequeue`` reclaims the complement of
#: and ``heartbeat`` renews for as long as the handler runs — and this is its third reader, after
#: ``object_write_leases``' own fence and the reconciler's orphan sweep. One definition of "the
#: holder is still alive" is the point: a second, hand-written copy here could drift from the one
#: the job runner actually enforces, and it would drift silently.
#:
#: Both halves are evaluated **by Postgres**, so no worker's clock enters the comparison — this
#: tree's ``now()`` is session-TZ rather than UTC.
_PIN_SQL = f"""SELECT EXISTS (SELECT 1 FROM rootfs_fetch_leases l
                              WHERE l.investigation_id = %s AND l.token = %s
                                AND {LIVE_HOLDER_SQL})"""  # noqa: S608 — module constant, not input

#: Retire this investigation's dead rows. Scoped to the investigation because the caller already
#: holds its ``INVESTIGATION`` advisory lock and is walking its checksums anyway, so the reap is
#: free there and needs no reconciler lane of its own. A dead row is inert before it is deleted —
#: :func:`fetch_lease_pins_base` already ignores it — so this is table-growth hygiene, and the
#: correctness of the gate does not depend on it having run.
#:
#: It carries the **same** :data:`LIVE_HOLDER_SQL` the gate does, verbatim, so the pass that honours
#: a lease and the pass that collects one cannot disagree about which leases are live: a reap looser
#: than the fence would delete a row that is actively protecting a multi-GiB download.
#: ``reap_stale_write_leases`` shares its own fence for the same reason.
_REAP_SQL = (  # noqa: S608 — LIVE_HOLDER_SQL is a module constant, never caller input
    f"DELETE FROM rootfs_fetch_leases l WHERE l.investigation_id = %s AND NOT {LIVE_HOLDER_SQL}"
)


def acquire_fetch_lease(
    conn: psycopg.Connection,
    investigation_id: UUID,
    token: str,
    *,
    system_id: UUID,
    job_id: UUID | None,
) -> UUID | None:
    """Record a live-holder fetch lease on an autocommit connection, or degrade to ``None``.

    Missing job identity, non-autocommit connections, and database faults are logged and leave the
    fetch unleased so a lease that cannot protect the transfer is never recorded.
    """
    if job_id is None:
        # Not an assertion, for `_flocked_partial`'s ENOLCK reason: a lane that reaches here has a
        # real provision to run, and refusing it would trade a rare, survivable reclaim race for a
        # total uploaded-rootfs outage. But it must be *loud*, because the mechanism is silently
        # absent for this fetch and the provision still succeeds.
        _log.warning(
            "no provision job id reached the rootfs fetch for system %s, so a lease would have no "
            "holder to be fenced on; staging unleased rather than recording a pin nothing can "
            "release — a concurrent reclaim reverts to its pre-ADR-0515 reach for this download",
            system_id,
        )
        return None
    if not conn.autocommit:
        # The one way this whole mechanism fails *silently and totally*, so it is checked rather
        # than assumed. Inside a transaction the row is invisible to the reclaim's separate
        # connection until commit — which, on the production path, is after the multi-GiB download
        # this lease exists to protect has already finished. The fetch would still succeed, the
        # reclaim would still race it, and nothing would raise: a conditional written down as an
        # invariant, which is the defect this subsystem's own docstrings keep naming. Only
        # ``rootfs_upload_fetch_from_env`` opens this connection and it passes ``autocommit=True``,
        # so reaching here means that changed and took ADR-0515 with it.
        _log.warning(
            "the rootfs fetch connection is not in autocommit, so a fetch lease would stay "
            "invisible to the reclaim until this transaction commits; staging unleased for system "
            "%s rather than recording a lease that pins nothing",
            system_id,
        )
        return None
    lease_id = uuid4()
    try:
        conn.execute(_ACQUIRE_SQL, (lease_id, investigation_id, token, system_id, job_id))
    except psycopg.Error as err:
        _log.warning(
            "could not record a rootfs fetch lease for %s in investigation %s (%s); staging "
            "unleased for system %s — a concurrent reclaim cannot see this download until its "
            "staging partial exists, which is the behavior before ADR-0515",
            token,
            investigation_id,
            err,
            system_id,
        )
        return None
    return lease_id


def release_fetch_lease(conn: psycopg.Connection, lease_id: UUID) -> None:
    """Release a fetch lease without replacing an in-flight exception on failure."""
    try:
        conn.execute(_RELEASE_SQL, (lease_id,))
    except psycopg.Error as err:
        _log.warning(
            "could not release rootfs fetch lease %s (%s); it pins its base until its holding "
            "job stops being a live claim, and the next reclaim after that drains the checksum",
            lease_id,
            err,
        )


async def fetch_lease_pins_base(conn: AsyncConnection, investigation_id: UUID, token: str) -> bool:
    """Return whether a lease whose holding job is still live pins this base."""
    async with conn.cursor() as cur:
        await cur.execute(_PIN_SQL, (investigation_id, token))
        row = await cur.fetchone()
    return bool(row and row[0])


async def reap_dead_fetch_leases(conn: AsyncConnection, investigation_id: UUID) -> None:
    """Delete inert leases whose holding job is no longer live."""
    await conn.execute(_REAP_SQL, (investigation_id,))
