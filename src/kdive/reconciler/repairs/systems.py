"""System-row repair for the reconciler."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import AllocationState, JobState, SystemState
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.control.control import detach_audit_event, detach_system_debug_sessions
from kdive.jobs.payloads import SystemPayload
from kdive.reconciler.repairs.allocations import SYSTEM_RECONCILER_PRINCIPAL
from kdive.security import audit

_log = logging.getLogger(__name__)

_ACTIVE_JOB_STATE_VALUES = (JobState.QUEUED.value, JobState.RUNNING.value)

_TERMINAL_ALLOCATION_STATES = (
    AllocationState.RELEASED,
    AllocationState.EXPIRED,
    AllocationState.FAILED,
)
_ORPHANED_SYSTEM_TERMINAL_STATES = (SystemState.TORN_DOWN, SystemState.FAILED)
_TERMINAL_ALLOCATION_STATE_VALUES = tuple(state.value for state in _TERMINAL_ALLOCATION_STATES)
_ORPHANED_SYSTEM_TERMINAL_STATE_VALUES = tuple(
    state.value for state in _ORPHANED_SYSTEM_TERMINAL_STATES
)
_TEARDOWN_JOB_KIND = JobKind.TEARDOWN


async def repair_orphaned_systems(conn: AsyncConnection) -> int:
    """Enqueue an idempotent GC teardown for each System whose Allocation is gone."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id, s.project FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "WHERE s.state <> ALL(%s) "
            "  AND a.state = ANY(%s)",
            (
                list(_ORPHANED_SYSTEM_TERMINAL_STATE_VALUES),
                list(_TERMINAL_ALLOCATION_STATE_VALUES),
            ),
        )
        candidates = await cur.fetchall()
    enqueued = 0
    for candidate in candidates:
        system_id: UUID = candidate["id"]
        dedup_key = f"{system_id}:teardown"
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
                fresh = await cur.fetchone()
                if fresh is None or fresh["state"] in _ORPHANED_SYSTEM_TERMINAL_STATE_VALUES:
                    continue
                await cur.execute("SELECT 1 FROM jobs WHERE dedup_key = %s", (dedup_key,))
                already_queued = await cur.fetchone() is not None
            await queue.enqueue(
                conn,
                _TEARDOWN_JOB_KIND,
                SystemPayload(system_id=str(system_id)),
                {
                    "principal": SYSTEM_RECONCILER_PRINCIPAL,
                    "agent_session": None,
                    "project": candidate["project"],
                },
                dedup_key,
            )
        if not already_queued:
            enqueued += 1
            _log.info("reconciler: orphaned system %s -> teardown job enqueued", system_id)
    return enqueued


def gone_system_state_values() -> tuple[str, ...]:
    """Return terminal System states used by collector GC."""
    return _ORPHANED_SYSTEM_TERMINAL_STATE_VALUES


async def repair_stalled_crashing_systems(conn: AsyncConnection) -> int:
    """Recover a `crashing` System whose force_crash job can never run again -> `crashed`.

    A `crashing` System's force_crash NMI has (overwhelmingly) already fired; if no force_crash
    job is still active (`queued`/`running`) — dead-lettered `failed`, operator-`canceled`, or the
    invariant-only absent row — the handler stopped before finalize, so the System would strand
    forever with power blocked (the R3 limbo). Resolve it evidence-first to `crashed` (ADR-0325):
    the crash workflow (`capture_vmcore` -> teardown) can then proceed. A still-active job — a
    `running` job (valid or lapsed lease with attempts remaining, which a worker re-dequeues) or a
    `queued` job mid-retry — is left to the normal retry path. Runs after `repair_abandoned_jobs`,
    which dead-letters a lease-lapsed-and-exhausted running job to `failed`.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id FROM systems s "
            "WHERE s.state = %s "
            "  AND NOT EXISTS ( "
            "    SELECT 1 FROM jobs j "
            "    WHERE j.dedup_key = s.id::text || ':force_crash' "
            "      AND j.state = ANY(%s) "
            "  )",
            (SystemState.CRASHING.value, list(_ACTIVE_JOB_STATE_VALUES)),
        )
        candidates = [row["id"] for row in await cur.fetchall()]
    recovered = 0
    for system_id in candidates:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            system = await SYSTEMS.get(conn, system_id)
            if system is None or system.state is not SystemState.CRASHING:
                continue
            await SYSTEMS.update_state(conn, system_id, SystemState.CRASHED)
            await audit.record_system(
                conn,
                principal=SYSTEM_RECONCILER_PRINCIPAL,
                event=audit.AuditEvent(
                    tool="control.force_crash",
                    object_kind="systems",
                    object_id=system_id,
                    transition="crashing->crashed",
                    args={"system_id": str(system_id)},
                    project=system.project,
                ),
            )
            await _detach_sessions_reconciler(conn, system)
        recovered += 1
        _log.info("reconciler: stalled crashing system %s -> crashed", system_id)
    return recovered


async def _detach_sessions_reconciler(conn: AsyncConnection, system: System) -> None:
    """Drive every non-terminal DebugSession of ``system`` to detached (reconciler principal).

    Reuses ``detach_system_debug_sessions``'s transition SQL; audits under the system principal
    (the reconciler has no request context) rather than under a job.
    """
    for session_id, old_state in await detach_system_debug_sessions(conn, system):
        await audit.record_system(
            conn,
            principal=SYSTEM_RECONCILER_PRINCIPAL,
            event=detach_audit_event(system, session_id, old_state),
        )
