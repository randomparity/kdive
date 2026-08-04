"""Provider-owned infrastructure repair for the reconciler."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.diagnostics.egress_probe import DEFAULT_PROBE_HEARTBEAT_STALE_AFTER
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.operations.jobs import JobKind
from kdive.providers.infra.console_hosting import CollectorRegistry
from kdive.providers.infra.reaping import DumpVolumeReaper, InfraReaper
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
    """Delete host_dump volumes orphaned by a non-graceful worker/host crash (ADR-0094)."""
    volumes = await reaper.list_dump_volumes()
    if not volumes:
        return 0
    cutoff_epoch = await _now_epoch(conn) - grace.total_seconds()
    reaped = 0
    for volume in volumes:
        if volume.mtime_epoch_s >= cutoff_epoch:
            continue
        if volume.system_id is not None and await has_active_capture_job(conn, volume.system_id):
            continue
        try:
            await reaper.delete_dump_volume(volume.name)
        except Exception:  # noqa: BLE001 - one volume failure must not starve the rest
            _log.warning(
                "reconciler: deleting orphaned dump volume %s failed; retry next pass",
                volume.name,
                exc_info=True,
            )
            continue
        reaped += 1
        _log.info("reconciler: reaped orphaned host_dump volume %s", volume.name)
    return reaped


async def _now_epoch(conn: AsyncConnection) -> float:
    """The Postgres clock as epoch seconds."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT extract(epoch from now())")
        row = await cur.fetchone()
    return float(row[0]) if row is not None else 0.0
