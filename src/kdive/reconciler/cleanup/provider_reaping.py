"""Provider-owned infrastructure repair for the reconciler."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import (
    LockScope,
    advisory_xact_lock,
    require_top_level_transaction,
    try_advisory_xact_lock,
)
from kdive.diagnostics.egress_probe import DEFAULT_PROBE_HEARTBEAT_STALE_AFTER
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.operations.jobs import JobKind
from kdive.providers.infra.console_hosting import CollectorRegistry
from kdive.providers.infra.reaping import DumpVolume, DumpVolumeReaper, InfraReaper
from kdive.providers.shared.host_dump_volume_leases import (
    has_live_host_dump_volume_lease,
    reap_stale_host_dump_volume_leases,
)
from kdive.providers.shared.runtime_paths import system_id_from_domain_name
from kdive.reconciler.repairs.allocations import has_active_capture_job
from kdive.reconciler.repairs.systems import gone_system_state_values

_log = logging.getLogger(__name__)

_TEARDOWN_JOB_IN_FLIGHT_STATE_VALUES = (JobState.QUEUED.value, JobState.RUNNING.value)
_TEARDOWN_JOB_KIND_VALUE = JobKind.TEARDOWN.value
_TORN_DOWN_SYSTEM_STATE_VALUE = SystemState.TORN_DOWN.value

DEFAULT_DUMP_VOLUME_GRACE = timedelta(minutes=30)


async def repair_leaked_domains(conn: AsyncConnection, reaper: InfraReaper) -> int:
    """Destroy provider domains whose owning System is gone and no teardown is in flight.

    The owning System is the domain's metadata tag when present, else the System encoded in
    its ``kdive-<uuid>`` name (ADR-0111): a genuinely orphaned domain that lost its tag but
    matches the naming convention is still ours, and is reaped once no live ``systems`` row
    backs it. A name that does not match the convention is foreign/unmanaged and never
    reaped. The metadata tag stays authoritative when present; the name is a fallback only.
    """
    domains = await reaper.list_owned()
    reaped = 0
    for domain in domains:
        system_id = domain.system_id or system_id_from_domain_name(domain.name)
        if system_id is None:
            continue  # not a kdive System domain → foreign/unmanaged → never reaped
        async with (
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT 1 FROM systems WHERE id = %s AND state <> %s",
                (system_id, _TORN_DOWN_SYSTEM_STATE_VALUE),
            )
            has_live_row = await cur.fetchone() is not None
            await cur.execute(
                "SELECT 1 FROM jobs WHERE state = ANY(%s) "
                "  AND kind = %s AND payload->>'system_id' = %s",
                (
                    list(_TEARDOWN_JOB_IN_FLIGHT_STATE_VALUES),
                    _TEARDOWN_JOB_KIND_VALUE,
                    str(system_id),
                ),
            )
            teardown_in_flight = await cur.fetchone() is not None
        if has_live_row or teardown_in_flight:
            continue
        try:
            await reaper.destroy(domain.name)
        except Exception:  # noqa: BLE001 - one domain's failure must not strand the others
            _log.warning(
                "reconciler: destroy of leaked domain %s failed; retry next pass",
                domain.name,
                exc_info=True,
            )
            continue
        reaped += 1
        _log.info("reconciler: leaked domain %s (system %s) reaped", domain.name, system_id)
    return reaped


class ProbeReaper(Protocol):
    """The narrow provider port the reconciler consumes to destroy a leaked probe guest.

    Structurally a subset of :class:`kdive.providers.infra.reaping.InfraReaper` (``destroy(name)``),
    so the reconciler reuses its existing reaper for both the leaked-domain and the
    leaked-probe sweep — a probe guest is destroyed by domain name like any other domain.
    """

    async def destroy(self, name: str) -> None: ...


async def repair_leaked_probe_guests(
    conn: AsyncConnection,
    reaper: ProbeReaper,
    *,
    heartbeat_stale_after: timedelta = DEFAULT_PROBE_HEARTBEAT_STALE_AFTER,
) -> int:
    """Reap ``guest_egress`` probe guests whose owning doctor run is gone; honor the heartbeat.

    A probe is leaked when its marker row is past its hard TTL **or** its active-run heartbeat
    is stale (the owning ``doctor`` run stopped beating) — and is not already released. A row
    with a **fresh** heartbeat is an in-use probe (a live run) and is **never** reaped (ADR-0091
    §3): the reaper must not destroy a guest mid-check and turn a healthy egress path into a
    spurious ``error``. On a successful destroy the row is stamped ``released_at`` so the
    provider's single-flight slot frees and a re-pass does not re-reap. Per-probe ``destroy``
    failures are isolated (one leak must not strand the others); time predicates run in
    Postgres (never a Python clock).
    """
    rows = await _leaked_probe_rows(conn, heartbeat_stale_after)
    reaped = 0
    for row in rows:
        if not await _destroy_probe(reaper, row):
            continue
        await _mark_probe_released(conn, row["id"])
        reaped += 1
        _log.info("reconciler: leaked egress probe %s reaped", row["domain_name"])
    return reaped


async def _leaked_probe_rows(conn: AsyncConnection, stale_after: timedelta) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, domain_name FROM egress_probe_guests "
            "WHERE released_at IS NULL "
            "  AND (ttl_deadline < now() OR heartbeat_at < now() - %s)",
            (stale_after,),
        )
        return list(await cur.fetchall())


async def _destroy_probe(reaper: ProbeReaper, row: dict) -> bool:
    try:
        await reaper.destroy(row["domain_name"])
    except Exception:  # noqa: BLE001 - one probe's failure must not strand the others
        _log.warning(
            "reconciler: destroy of leaked egress probe %s failed; retry next pass",
            row["domain_name"],
            exc_info=True,
        )
        return False
    return True


async def _mark_probe_released(conn: AsyncConnection, probe_id: UUID) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE egress_probe_guests SET released_at = now() WHERE id = %s", (probe_id,)
        )


async def reap_console_collectors(conn: AsyncConnection, registry: CollectorRegistry) -> int:
    """Finalize and drop console collectors for gone Systems (ADR-0095)."""
    held = registry.system_ids()
    if not held:
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, state FROM systems WHERE id = ANY(%s)",
            (list(held),),
        )
        states = {row[0]: row[1] for row in await cur.fetchall()}
    reaped = 0
    gone_states = gone_system_state_values()
    for system_id in held:
        state = states.get(system_id)
        if state is not None and state not in gone_states:
            continue
        await registry.finalize_and_drop_async(system_id)
        reaped += 1
        _log.info("reconciler: console collector for gone system %s finalized + reaped", system_id)
    return reaped


async def reap_orphaned_dump_volumes(
    conn: AsyncConnection, reaper: DumpVolumeReaper, grace: timedelta
) -> int:
    """Delete host_dump volumes orphaned by a non-graceful worker/host crash (ADR-0094, ADR-0562).

    Each candidate's final classification and its delete run in **one transaction holding
    ``(SYSTEM, system_id)``** — the lock ``hold_host_dump_volume_lease`` mints under. A capture
    therefore either declares itself before this pass classifies (and is seen), or blocks until the
    delete is done and then recreates a volume this pass has already passed over. Without that the
    guards are a state sample: ADR-0557 deliberately excludes a ``queued`` job, so a worker claiming
    the job between the check and the delete left the answer stale, and the capture's own
    delete-stale-then-dump pair put a **new** volume at the same deterministic name for the delete
    to resolve onto (#1955).

    Stale leases are collected first, ahead of the volume list, so a deployment whose reaper owns no
    volumes still drains rows a failed capture left behind.

    The transaction-free precondition is asserted **here**, not only per volume: the stale-lease
    collection runs before the volume list, and on a connection already in a transaction it
    would degrade to a savepoint that commits nothing. With an empty volume list the pass would then
    return ``0`` having silently discarded that work, never reaching the per-volume assertion.

    Returns:
        The number of volumes deleted. Skips — a contended System, a live holder, a volume in the
        grace window, a volume whose identity changed, a volume no reachable host held — are not
        counted and are not faults.
    """
    require_top_level_transaction(conn, "the host_dump orphan sweep")
    await reap_stale_host_dump_volume_leases(conn)
    volumes = await reaper.list_dump_volumes()
    if not volumes:
        return 0
    # In its own transaction, so the connection is idle again afterwards. On the reconciler's
    # non-autocommit pool connection a bare `execute` opens a transaction that lives until the pool
    # takes the connection back, after which every per-volume `conn.transaction()` below would be a
    # savepoint that commits nothing and every `pg_advisory_xact_lock` would be held for the whole
    # pass rather than for one volume (ADR-0005; the same hazard `require_top_level_transaction`
    # exists for).
    async with conn.transaction():
        cutoff_epoch = await _now_epoch(conn) - grace.total_seconds()
    reaped = 0
    for volume in volumes:
        if volume.mtime_epoch_s >= cutoff_epoch:
            continue
        if await _delete_if_still_orphaned(conn, reaper, volume):
            reaped += 1
    return reaped


async def _delete_if_still_orphaned(
    conn: AsyncConnection, reaper: DumpVolumeReaper, volume: DumpVolume
) -> bool:
    """Re-classify ``volume`` and delete it, both under the System's advisory lock (ADR-0562).

    The acquire is a ``try``: a contended System is one a capture is declaring itself on now, so
    the volume is skipped and the next pass re-derives it. A blocking acquire would let one holder
    stall a reconciler pass that has no deadline, behind allocation expiry and System repair
    — the trade ADR-0502 item 4 makes for the same reason. The skip is deliberately not a counted
    fault, so a wedged holder defers this System's volume on every pass while the count reads clean;
    the INFO line is the whole of the signal.

    A volume whose name carries no parseable System UUID has no lock to take and keeps its age-only
    classification; its delete is still identity-addressed.
    """
    require_top_level_transaction(conn, "the host_dump orphan sweep's per-volume fence")
    async with conn.transaction():
        if volume.system_id is not None:
            if not await try_advisory_xact_lock(conn, LockScope.SYSTEM, volume.system_id):
                _log.info(
                    "reconciler: system %s is locked by an active operation; deferring dump "
                    "volume %s to the next pass",
                    volume.system_id,
                    volume.name,
                )
                return False
            # Two holders, not one mechanism twice, but they overlap heavily: `dequeue` commits
            # `running` before the handler runs, so ADR-0557's predicate already covers the whole
            # interval from the claim to the last provider call. What the lease adds is that it is
            # keyed on the System directly. ADR-0557's predicate reaches the System only through
            # `runs.id::text = jobs.payload->>'run_id'`, so a Run row deleted mid-capture unfences a
            # live capture; the lease row does not depend on that join. What ADR-0557's predicate
            # adds is a `running` job whose *job lease* has lapsed — the queue has given up on it,
            # which makes the lease row no longer live, while its libvirt thread may still be
            # writing. Neither is redundant, and neither alone covers both.
            if await has_live_host_dump_volume_lease(conn, volume.system_id):
                return False
            if await has_active_capture_job(conn, volume.system_id):
                return False
        try:
            reclaimed = await reaper.delete_dump_volume(
                volume.name, expected_mtime_epoch_s=volume.mtime_epoch_s
            )
        except Exception:  # noqa: BLE001 - one volume failure must not starve the rest
            _log.warning(
                "reconciler: deleting orphaned dump volume %s failed; retry next pass",
                volume.name,
                exc_info=True,
            )
            return False
    if not reclaimed:
        # Either the provider found a different volume under this name than the one classified and
        # left it alone, or no reachable host held the name at all. Neither reclaimed anything, and
        # counting it would report reclamation that did not happen; the provider logs which case it
        # was, so nothing is added here.
        return False
    _log.info("reconciler: reaped orphaned host_dump volume %s", volume.name)
    return True


async def _now_epoch(conn: AsyncConnection) -> float:
    """The Postgres clock as epoch seconds."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT extract(epoch from now())")
        row = await cur.fetchone()
    return float(row[0]) if row is not None else 0.0
