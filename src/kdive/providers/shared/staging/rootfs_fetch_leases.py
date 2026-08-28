"""The durable in-flight marker an uploaded-rootfs fetch holds while it stages (ADR-0515, #1702).

#1558's option 2. The ADR-0441 §6 pin classifier decides whether a System pins its base from the
System's state column plus overlay presence, and the two terminal states a doomed provision reaches
both defeat it: ``_ROOTFS_REFERENCERS_SQL`` excludes ``torn_down`` outright, ``FAILED`` is outside
:data:`~kdive.domain.capacity.state.ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`, and a System that died
mid-fetch never got an overlay. ADR-0495 answered the *transfer* half by asking the kernel about a
held ``flock`` on the fetcher's partial; what it left open is the window before the partial exists,
which is where a fetch waits on its per-(investigation, checksum) session lock.

A row here is that window's evidence. The fetch inserts one before it resolves its ``artifacts``
row and deletes it on every unwind; the reclaim's per-checksum gate asks whether any row exists for
this ``(investigation_id, token)`` **whose holding job is still a live claim**.

That last clause is ADR-0522 (#1740), and it is what a reader coming from ADR-0515 will not expect.
The row carried a 6-hour ``expires_at`` because the provision seam handed the fetch a
``RootfsUploadContext`` with no job identity, leaving nothing to fence on; #1740 threaded the
provision job's id through that seam, so the fence is now ``object_write_leases``' (ADR-0502): the
pin is valid exactly while the holder's own heartbeat-renewed ``jobs.lease_expires_at`` is. There is
no derived constant here any more, and a fetcher killed by ``SIGKILL`` stops pinning its base on the
job-lease interval rather than on a worst-case transfer estimate.

Placed under ``providers.shared`` for
:mod:`~kdive.providers.shared.staging.staging_partials`'s reason: one caller is a provider
lifecycle path and the other is a job handler, and ``src/kdive/jobs/`` must not
reach into a provider's lifecycle package. The two sides also differ in connection flavour — the
fetch holds a **sync** autocommit ``psycopg`` connection, the reclaim an **async** one — so the SQL
lives here once rather than being written twice against two drivers and drifting.
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
    """Record that this fetcher is staging ``token``; return the lease id, or ``None`` on a fault.

    Called on the fetch's own **autocommit** connection, so the row is visible to the reclaim's
    separate connection the moment this returns — which it must be, since the two are not
    serialized by anything (the fetch takes only its session lock, never the ``INVESTIGATION`` lock
    the reclaim holds).

    A fault degrades to an unleased fetch with a ``WARNING`` rather than failing the provision, on
    :func:`~kdive.providers.local_libvirt.lifecycle.rootfs.upload_acquisition._flocked_partial`'s
    own ``ENOLCK`` precedent. The pin is advisory: without it the reclaim reverts to exactly its
    pre-ADR-0515 reach, which is a race that is rare and survivable, whereas failing here would turn
    any transient database blip into a total uploaded-rootfs provisioning outage. The caller must
    treat ``None`` as "no lease to release".

    Args:
        conn: The fetch's own autocommit sync connection.
        investigation_id: The investigation whose base is being staged.
        token: The content-address token of that base.
        system_id: The provisioning System, for attribution.
        job_id: The provision job this fetch runs under — the holder whose liveness *is* the
            lease's (ADR-0522). ``None`` is the same degrade as a database fault: no row is
            recorded, because a lease with no live holder to test against is the unbounded pin
            ADR-0515 §3 spent a derived deadline to avoid. Reachable only from a lane that
            materializes an ``upload`` rootfs outside a job, which no production caller does.
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
    """Drop this fetcher's lease; a fault is reported and left to the holding job's own lease.

    Runs from the fetch's ``finally``, so it must not raise: raising out of a ``finally`` replaces
    the in-flight exception and would demote an actionable ``CategorizedError`` — a checksum
    mismatch, a non-qcow2 upload — to ``__context__`` behind a Postgres message. That is the defect
    :func:`~kdive.providers.local_libvirt.lifecycle.rootfs.upload_acquisition._release_fetch_lock`
    documents and fixes for the advisory lock one call away, and it arrives here for the same reason
    — the connection is frequently *already gone* by the time this runs, which is precisely the
    crash shape the fence exists to bound.
    """
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
    """Whether a **live-held** fetch lease pins ``token``'s base in ``investigation_id``.

    The reclaim gate's ADR-0515 question, and the one that closes ADR-0495's residual window 2.

    The holder's liveness is the whole reason this cannot be a bare existence test. A fetcher killed
    by ``SIGKILL`` leaves its row behind and nothing in this module clears it — ``failed`` is
    terminal with no transition out of it, ``torn_down`` is the achieved post-state, and no
    reconciler repair reaches a lease. Treating a bare row as a pin would therefore pin a base
    **forever** on the one path that matters, which is the disk-exhaustion regression
    ``test_failed_referencer_with_overlay_gone_drains`` (AC-8) exists to catch.

    What keeps that bounded is the holding job's own heartbeat-renewed lease (ADR-0522), evaluated
    in the same statement rather than by a caller that might forget it. A killed worker stops
    renewing, so the pin lapses on the job-lease interval — not on the worst-case transfer estimate
    ADR-0515 §4 derived, which this replaced.
    """
    async with conn.cursor() as cur:
        await cur.execute(_PIN_SQL, (investigation_id, token))
        row = await cur.fetchone()
    return bool(row and row[0])


async def reap_dead_fetch_leases(conn: AsyncConnection, investigation_id: UUID) -> None:
    """Delete ``investigation_id``'s leases whose holding job is no longer live; hygiene only.

    Such a lease is already inert to :func:`fetch_lease_pins_base`, so nothing observable depends on
    this having run — it exists so a host that repeatedly kills fetchers does not accumulate dead
    rows for the life of the investigation. Run from the reclaim job, which already holds this
    investigation's ``INVESTIGATION`` advisory lock and is walking its checksums, so it costs one
    indexed delete and needs no reconciler lane of its own.

    ``ON DELETE CASCADE`` on ``job_id`` already collects a lease whose ``jobs`` row is deleted; this
    collects the far commoner case of a job that merely stopped running.
    """
    await conn.execute(_REAP_SQL, (investigation_id,))
