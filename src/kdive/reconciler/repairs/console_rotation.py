"""Reconciler dispatch of the ``console_rotate`` job for live local Systems (#892).

Each pass enqueues one ``console_rotate`` worker job per booted local-libvirt System that has no
pending/running rotation job, so the worker keeps rotating the System's growing console into
redacted part artifacts (ADR-0223) for as long as the System is live. Liveness is keyed on the
System, never on a Run's terminality: the #892 repro had a ``ready`` System whose most recent Run
had already ``succeeded`` while the in-guest workload kept emitting console — a terminal Run must
not stop rotation, which continues until the System is torn down.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import ConsoleRotatePayload
from kdive.providers.shared.runtime_paths import console_log_path
from kdive.reconciler.repairs.allocations import SYSTEM_RECONCILER_PRINCIPAL

_log = logging.getLogger(__name__)

# A System whose libvirt domain has booted and is not yet torn down: its console log is growing
# (or holds a captured crash), so rotation must keep running. `defined`/`provisioning` have not
# booted; `reprovisioning` is mid-rebuild with the domain down; `torn_down`/`failed` are gone.
_LIVE_SYSTEM_STATES: tuple[str, ...] = (SystemState.READY.value, SystemState.CRASHED.value)
_LOCAL_PROVIDER = ResourceKind.LOCAL_LIBVIRT.value
_IN_FLIGHT_JOB_STATES: tuple[str, ...] = (JobState.QUEUED.value, JobState.RUNNING.value)

_LIVE_LOCAL_SYSTEMS_SQL = (
    "SELECT s.id, s.project FROM systems s "
    "JOIN allocations a ON a.id = s.allocation_id "
    "JOIN resources r ON r.id = a.resource_id "
    "WHERE r.kind = %s AND s.state = ANY(%s)"
)
_IN_FLIGHT_ROTATION_SQL = (
    "SELECT 1 FROM jobs WHERE kind = %s AND state = ANY(%s) AND payload->>'system_id' = %s LIMIT 1"
)


def _boot_id(system_id: UUID) -> str:
    """Return a per-boot identity for ``system_id``'s console log, or ``""`` if it cannot stat.

    Uses the console file's ``os.stat`` identity ``dev:ino:mtime`` — independent of the log's
    size, so a power-cycle (which truncates/recreates the log, ADR-0258) changes it even when the
    new boot has already grown past the prior cursor offset. The reconciler may not be co-located
    with the worker that owns the file, so any stat failure (not co-located / unreadable / absent)
    degrades to ``""``: the handler treats ``""`` as a reset-forcing identity and still sees the
    real file on the worker, where its own shrink check catches the common truncation case.
    """
    try:
        st = os.stat(console_log_path(system_id))
    except OSError:
        return ""
    return f"{st.st_dev}:{st.st_ino}:{int(st.st_mtime)}"


async def _has_in_flight_rotation(conn: AsyncConnection, system_id: UUID) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            _IN_FLIGHT_ROTATION_SQL,
            (JobKind.CONSOLE_ROTATE.value, list(_IN_FLIGHT_JOB_STATES), str(system_id)),
        )
        return await cur.fetchone() is not None


async def _enqueue_rotation(conn: AsyncConnection, system_id: UUID, project: str) -> None:
    await queue.enqueue(
        conn,
        JobKind.CONSOLE_ROTATE,
        ConsoleRotatePayload(system_id=str(system_id), boot_id=_boot_id(system_id)),
        {"principal": SYSTEM_RECONCILER_PRINCIPAL, "agent_session": None, "project": project},
        f"console_rotate:{system_id}:{uuid4()}",
    )


async def sweep_console_rotation(conn: AsyncConnection) -> int:
    """Enqueue a ``console_rotate`` job for each live local System with none in flight.

    Selects booted local-libvirt Systems (``ready``/``crashed``, via the resource-kind join),
    skips any that already have a pending/running ``console_rotate`` job (best-effort dedup; the
    handler's per-System advisory lock and insert-if-absent part keys make a duplicate a safe
    no-op), and enqueues one job carrying the System id and the console log's per-boot ``boot_id``.
    Each enqueue carries a unique ``dedup_key`` so a new job follows once the prior one finishes —
    rotation is continuous, not one-shot. Returns the number of jobs enqueued this pass.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_LIVE_LOCAL_SYSTEMS_SQL, (_LOCAL_PROVIDER, list(_LIVE_SYSTEM_STATES)))
        candidates = await cur.fetchall()
    enqueued = 0
    for candidate in candidates:
        system_id: UUID = candidate["id"]
        async with conn.transaction():
            if await _has_in_flight_rotation(conn, system_id):
                continue
            await _enqueue_rotation(conn, system_id, candidate["project"])
        enqueued += 1
        _log.info("reconciler: live system %s -> console_rotate job enqueued", system_id)
    return enqueued


__all__ = ["sweep_console_rotation"]
