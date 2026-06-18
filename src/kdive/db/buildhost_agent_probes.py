"""Async repository for buildhost_agent_probe_guests reaper markers (ADR-0167).

The `ephemeral_libvirt_buildhost_agent` doctor check (ADR-0167) provisions a throwaway
`kdive-build-<run_id>` builder per ephemeral_libvirt host. Because that builder is a real build-VM
domain the reconciler's `reap_orphan_build_vms` sweep already owns — and a doctor probe has no BUILD
job to prove liveness — the probe registers a marker here under the builder's `run_id`, carrying an
active-run `heartbeat_at` and a hard `ttl_deadline`. `is_probe_live` is the predicate that sweep
consults: a build VM whose run_id has a fresh, unreleased probe heartbeat is live and is not reaped.
The partial unique index on `build_host_id` (live rows only) is the cross-process single-flight
fence.

All time predicates evaluate `now()` in Postgres, never a Python clock (the `provider_reaping`
convention), so the reconciler and the probe agree on staleness regardless of clock skew. The TTL
and staleness defaults mirror the sibling `diagnostics.egress_probe` mutating-probe tuning
(ADR-0091) but are owned here: ``db`` is a lower layer than ``diagnostics``, so this module must not
import it; the small constant/exception duplication between two independent probe subsystems is the
cost of a clean layering direction (consumers in ``diagnostics`` import these downward).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# The hard TTL backstop and the heartbeat staleness window mirror diagnostics.egress_probe's tuning
# (ADR-0091): the TTL is sized well above a probe's max runtime so the reaper never destroys a live
# builder mid-check; staleness is below the TTL so a stalled run is reaped promptly.
DEFAULT_PROBE_TTL = timedelta(minutes=10)
DEFAULT_PROBE_HEARTBEAT_STALE_AFTER = timedelta(minutes=2)


class ProbeInFlightError(Exception):
    """A live probe row already exists for this build host — the DB single-flight fence fired.

    Distinct from a backend-down error so the check can report "a probe is already in flight" rather
    than a generic registration failure (the cross-process second-caller signal).
    """


__all__ = [
    "DEFAULT_PROBE_HEARTBEAT_STALE_AFTER",
    "DEFAULT_PROBE_TTL",
    "ProbeInFlightError",
    "heartbeat",
    "is_probe_live",
    "register",
    "release",
]


async def register(
    pool: AsyncConnectionPool,
    *,
    build_host_id: UUID,
    run_id: UUID,
    ttl: timedelta = DEFAULT_PROBE_TTL,
) -> UUID:
    """Insert a live marker row for the probe builder; return its id.

    Args:
        pool: An open async connection pool.
        build_host_id: The ephemeral_libvirt build host being probed (the single-flight key).
        run_id: The builder's Run id (names the ``kdive-build-<run_id>`` domain; the reaper key).
        ttl: The hard TTL backstop before the marker is reapable regardless of heartbeat.

    Returns:
        The new marker row's id.

    Raises:
        ProbeInFlightError: a live probe row already exists for ``build_host_id`` (the partial
            unique index fired — the cross-process single-flight fence).
    """
    try:
        async with (
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "INSERT INTO buildhost_agent_probe_guests (build_host_id, run_id, ttl_deadline) "
                "VALUES (%s, %s, now() + %s) RETURNING id",
                (build_host_id, run_id, ttl),
            )
            row = await cur.fetchone()
    except UniqueViolation as exc:
        raise ProbeInFlightError(str(build_host_id)) from exc
    if row is None:  # invariant: INSERT ... RETURNING always yields one row
        raise RuntimeError("INSERT into buildhost_agent_probe_guests returned no row")
    return row["id"]


async def heartbeat(pool: AsyncConnectionPool, probe_id: UUID) -> None:
    """Advance the active-run heartbeat so the reaper never mistakes a live probe for a leak."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE buildhost_agent_probe_guests SET heartbeat_at = now() WHERE id = %s",
            (probe_id,),
        )


async def release(pool: AsyncConnectionPool, probe_id: UUID) -> None:
    """Stamp ``released_at`` so the host's single-flight slot frees for the next run."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE buildhost_agent_probe_guests SET released_at = now() "
            "WHERE id = %s AND released_at IS NULL",
            (probe_id,),
        )


async def is_probe_live(
    conn: AsyncConnection,
    run_id: UUID,
    *,
    stale_after: timedelta = DEFAULT_PROBE_HEARTBEAT_STALE_AFTER,
) -> bool:
    """Whether a probe builder for ``run_id`` is live: fresh heartbeat, unreleased, before TTL.

    The staleness window and TTL are evaluated in Postgres (``now()``), matching the reaper's
    clock-in-DB convention so a live probe is never reaped on clock skew.

    Args:
        conn: An async connection (the reconciler reuses its sweep connection).
        run_id: The builder's Run id encoded in the ``kdive-build-<run_id>`` domain name.
        stale_after: How long since the last heartbeat before the probe is treated as a leak.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM buildhost_agent_probe_guests "
            "WHERE run_id = %s AND released_at IS NULL "
            "  AND heartbeat_at > now() - %s AND now() < ttl_deadline",
            (run_id, stale_after),
        )
        return await cur.fetchone() is not None
