"""The durable in-flight marker an uploaded-rootfs fetch holds while it stages (ADR-0515, #1702).

#1558's option 2. The ADR-0441 §6 pin classifier decides whether a System pins its base from the
System's state column plus overlay presence, and the two terminal states a doomed provision reaches
both defeat it: ``_ROOTFS_REFERENCERS_SQL`` excludes ``torn_down`` outright, ``FAILED`` is outside
:data:`~kdive.domain.capacity.state.ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`, and a System that died
mid-fetch never got an overlay. ADR-0495 answered the *transfer* half by asking the kernel about a
held ``flock`` on the fetcher's partial; what it left open is the window before the partial exists,
which is where a fetch waits on its per-(investigation, checksum) session lock.

A row here is that window's evidence. The fetch inserts one before it resolves its ``artifacts``
row and deletes it on every unwind; the reclaim's per-checksum gate asks whether any **unexpired**
row exists for this ``(investigation_id, token)``.

Placed under ``providers.shared`` for :mod:`~kdive.providers.shared.staging_partials`'s reason: one
caller is a provider lifecycle path and the other is a job handler, and ``src/kdive/jobs/`` must not
reach into a provider's lifecycle package. The two sides also differ in connection flavour — the
fetch holds a **sync** autocommit ``psycopg`` connection, the reclaim an **async** one — so the SQL
lives here once rather than being written twice against two drivers and drifting.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
from psycopg import AsyncConnection

_log = logging.getLogger(__name__)

#: The canonical per-object cap a staged base is bounded by — the ``KDIVE_MAX_UPLOAD_BYTES``
#: default and ``uploads._SYSTEM_UNCOMPRESSED_CAP``, restated here as the numerator of the TTL
#: derivation below rather than imported, because importing an MCP tool module into a provider path
#: to read one integer would invert the dependency direction for no benefit.
_CANONICAL_BASE_CAP_BYTES = 50 * 1024**3

#: The floor sustained staging throughput the TTL is derived against. Deliberately about an order of
#: magnitude below what a healthy host achieves against a same-LAN S3-compatible store: the TTL must
#: not be the thing that fires on a slow-but-working transfer, because expiring under a live fetcher
#: silently reopens the very race this lease closes — the worst failure a fence can have (ADR-0502).
#: It is a floor chosen for that asymmetry, not a measured rate.
_FLOOR_STAGING_THROUGHPUT_BYTES_S = 5 * 1024**2

#: How long a fetch lease pins its base without being released.
#:
#: Derived, not picked. One full-cap transfer at the floor rate is
#: ``50 GiB / 5 MiB/s = 10240 s`` (2 h 51 m). The lease is taken *before* the per-(investigation,
#: checksum) session lock — which is the whole point of its placement, since that wait is the window
#: ADR-0495 could not see — so a fetcher can legitimately hold it for a sibling's entire transfer
#: and then its own when that sibling fails without publishing: ``2 x 10240 s`` (5 h 41 m), rounded
#: up to 6 h. That lands on the same magnitude as ``ROOTFS_STAGING_DRAIN_BACKOFF``, which is a
#: sanity check rather than the derivation.
#:
#: **This interval is the residual leak window, stated as such.** A fetcher killed by ``SIGKILL``
#: releases nothing — it has no heartbeat, and ``failed``/``torn_down`` are terminal, so nothing
#: else ever clears its row — and its base, its object and its ``artifacts`` row are all retained
#: until the lease expires. That is the accepted cost of keeping this evidence in the database:
#: bounded, visible in ``rootfs_fetch_leases``, and reclaimed on the first pass after expiry.
#: Shortening it trades that window against reopening the race; there is no value that does neither.
ROOTFS_FETCH_LEASE_TTL = timedelta(hours=6)

_ACQUIRE_SQL = (
    "INSERT INTO rootfs_fetch_leases (id, investigation_id, token, system_id, expires_at) "
    "VALUES (%s, %s, %s, %s, now() + %s)"
)
_RELEASE_SQL = "DELETE FROM rootfs_fetch_leases WHERE id = %s"

#: The pin probe. ``EXISTS`` rather than a count: the gate needs one bit and the index makes this a
#: range scan that stops at the first live row. ``expires_at > now()`` is evaluated **by Postgres**,
#: so no worker's clock enters the comparison — this tree's ``now()`` is session-TZ rather than UTC,
#: and a Python-side deadline computed against a drifting worker clock would expire a live lease
#: early on exactly the hosts where that drift is worst.
_PIN_SQL = (
    "SELECT EXISTS (SELECT 1 FROM rootfs_fetch_leases "
    "WHERE investigation_id = %s AND token = %s AND expires_at > now())"
)

#: Retire this investigation's expired rows. Scoped to the investigation because the caller already
#: holds its ``INVESTIGATION`` advisory lock and is walking its checksums anyway, so the reap is
#: free there and needs no reconciler lane of its own. An expired row is inert before it is deleted
#: — :func:`fetch_lease_pins_base` already ignores it — so this is table-growth hygiene, and the
#: correctness of the gate does not depend on it having run.
_REAP_SQL = "DELETE FROM rootfs_fetch_leases WHERE investigation_id = %s AND expires_at <= now()"


def acquire_fetch_lease(
    conn: psycopg.Connection, investigation_id: UUID, token: str, *, system_id: UUID
) -> UUID | None:
    """Record that this fetcher is staging ``token``; return the lease id, or ``None`` on a fault.

    Called on the fetch's own **autocommit** connection, so the row is visible to the reclaim's
    separate connection the moment this returns — which it must be, since the two are not
    serialized by anything (the fetch takes only its session lock, never the ``INVESTIGATION`` lock
    the reclaim holds).

    A fault degrades to an unleased fetch with a ``WARNING`` rather than failing the provision, on
    :func:`~kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch._flocked_partial`'s
    own ``ENOLCK`` precedent. The pin is advisory: without it the reclaim reverts to exactly its
    pre-ADR-0515 reach, which is a race that is rare and survivable, whereas failing here would turn
    any transient database blip into a total uploaded-rootfs provisioning outage. The caller must
    treat ``None`` as "no lease to release".
    """
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
        conn.execute(
            _ACQUIRE_SQL, (lease_id, investigation_id, token, system_id, ROOTFS_FETCH_LEASE_TTL)
        )
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
    """Drop this fetcher's lease; a fault is reported and left to the TTL.

    Runs from the fetch's ``finally``, so it must not raise: raising out of a ``finally`` replaces
    the in-flight exception and would demote an actionable ``CategorizedError`` — a checksum
    mismatch, a non-qcow2 upload — to ``__context__`` behind a Postgres message. That is the defect
    :func:`~kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch._release_fetch_lock`
    documents and fixes for the advisory lock one call away, and it arrives here for the same reason
    — the connection is frequently *already gone* by the time this runs, which is precisely the
    crash shape the TTL exists to bound.
    """
    try:
        conn.execute(_RELEASE_SQL, (lease_id,))
    except psycopg.Error as err:
        _log.warning(
            "could not release rootfs fetch lease %s (%s); it pins its base until it expires "
            "(TTL %s), and the next reclaim after that drains the checksum normally",
            lease_id,
            err,
            ROOTFS_FETCH_LEASE_TTL,
        )


async def fetch_lease_pins_base(conn: AsyncConnection, investigation_id: UUID, token: str) -> bool:
    """Whether an **unexpired** fetch lease pins ``token``'s base in ``investigation_id``.

    The reclaim gate's ADR-0515 question, and the one that closes ADR-0495's residual window 2.

    Expiry is the whole reason this cannot be a bare existence test. A fetcher killed by ``SIGKILL``
    leaves its row behind and nothing in this system clears it — ``failed`` is terminal with no
    transition out of it, ``torn_down`` is the achieved post-state, and no reconciler repair reaches
    a lease. Treating a bare row as a pin would therefore pin a base **forever** on the one path
    that matters, which is the disk-exhaustion regression
    ``test_failed_referencer_with_overlay_gone_drains`` (AC-8) exists to catch. The deadline is what
    keeps that bounded, so it is evaluated in the same statement rather than by a caller that might
    forget it.
    """
    async with conn.cursor() as cur:
        await cur.execute(_PIN_SQL, (investigation_id, token))
        row = await cur.fetchone()
    return bool(row and row[0])


async def reap_expired_fetch_leases(conn: AsyncConnection, investigation_id: UUID) -> None:
    """Delete ``investigation_id``'s expired lease rows; hygiene only, never correctness.

    An expired lease is already inert to :func:`fetch_lease_pins_base`, so nothing observable
    depends on this having run — it exists so a host that repeatedly kills fetchers does not
    accumulate dead rows for the life of the investigation. Run from the reclaim job, which already
    holds this investigation's ``INVESTIGATION`` advisory lock and is walking its checksums, so it
    costs one indexed delete and needs no reconciler lane of its own.
    """
    await conn.execute(_REAP_SQL, (investigation_id,))
