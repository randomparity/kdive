"""System-row repair for the reconciler."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import SNAPSHOTS, SYSTEMS, record_system_failure_category
from kdive.domain.capacity.state import AllocationState, JobState, SnapshotState, SystemState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import SystemPayload
from kdive.reconciler.repairs.allocations import SYSTEM_RECONCILER_PRINCIPAL
from kdive.security import audit
from kdive.services.debug.detach import detach_audit_event, detach_system_debug_sessions

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


async def repair_stalled_restoring_systems(conn: AsyncConnection) -> int:
    """Recover a `restoring` System whose restore job can never run again -> `failed` (ADR-0378).

    A `restoring` System is fenced (reprovision/power/teardown/snapshot all require `ready`), so a
    restore whose worker died mid-revert, dead-lettered, or was canceled would strand the System in
    `restoring` forever — the R3 limbo. If no `restore` job for this System is still active
    (`queued`/`running`), the handler stopped before committing, and a half-reverted guest is
    indeterminate, so resolve to `failed` (never back to `ready`). A still-active job is left to the
    normal retry path. Runs after `repair_abandoned_jobs`, which dead-letters an exhausted job.

    The `failed` transition carries `restore_incomplete` on `systems.failure_category`, written in
    this transaction (ADR-0513). Without it the System reported the flattened
    `infrastructure_failure` default — and its `retryable: true`, which invites an agent to
    re-drive a System whose disk is indeterminate and which is fenced from every lifecycle op
    anyway. It is stamped only when the restore job explains nothing itself; see
    `_restore_limbo_category`.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id, s.project FROM systems s "
            "WHERE s.state = %s "
            "  AND NOT EXISTS ( "
            "    SELECT 1 FROM jobs j "
            "    WHERE j.kind = %s "
            "      AND j.payload->>'system_id' = s.id::text "
            "      AND j.state = ANY(%s) "
            "  )",
            (SystemState.RESTORING.value, JobKind.RESTORE.value, list(_ACTIVE_JOB_STATE_VALUES)),
        )
        candidates = [(row["id"], row["project"]) for row in await cur.fetchall()]
    recovered = 0
    for system_id, project in candidates:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            system = await SYSTEMS.get(conn, system_id)
            if system is None or system.state is not SystemState.RESTORING:
                continue
            await SYSTEMS.update_state(conn, system_id, SystemState.FAILED)
            category = await _restore_limbo_category(conn, system_id)
            if category is not None:
                await record_system_failure_category(conn, system_id, category)
            await audit.record_system(
                conn,
                principal=SYSTEM_RECONCILER_PRINCIPAL,
                event=audit.AuditEvent(
                    tool="systems.restore",
                    object_kind="systems",
                    object_id=system_id,
                    transition="restoring->failed",
                    args={"system_id": str(system_id)},
                    project=project,
                ),
            )
        recovered += 1
        _log.info("reconciler: stalled restoring system %s -> failed", system_id)
    return recovered


async def _restore_limbo_category(conn: AsyncConnection, system_id: UUID) -> ErrorCategory | None:
    """Return the verdict to record for a stalled restore, or ``None`` to record none (ADR-0513).

    This repair's evidence is an **absence** — no `restore` job can run again — so unlike the job
    handler, which classifies a caught exception, it can be holding strictly weaker information
    than the job row. `_resolve_failure_verdict` gives a recorded category precedence over the
    job's, so stamping unconditionally would let `restore_incomplete` displace a real reason. Two
    live paths produce one: `restore_handler` binds its snapshotter (and loads its payload)
    *before* the `try` that routes `restoring -> failed`, so a `CategorizedError` there dead-letters
    the job with its own category and never touches System state; and `_record_system_failure` is
    best-effort, so a swallowed write leaves the System `restoring` beside a job holding both the
    category and the redacted message.

    So record `restore_incomplete` only when the newest terminal `restore` job explains nothing:
    absent, or carrying no category, or carrying `lease_expired` — which `repair_abandoned_jobs`
    stamps unconditionally over a job that never recorded a reason of its own, and is a statement
    about the lease rather than about the guest. Otherwise return ``None`` and leave the column
    NULL, which is exactly the ADR-0454 job fallback this repair should defer to.

    The lookup is keyed by the ``jobs.payload->>'system_id'`` expression index (migration 0082) and
    runs once per System actually recovered, inside the transaction that already holds its lock.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT j.error_category FROM jobs j "
            "WHERE j.kind = %s "
            "  AND j.payload->>'system_id' = %s "
            "  AND j.state <> ALL(%s) "
            "ORDER BY j.created_at DESC, j.id DESC LIMIT 1",
            (JobKind.RESTORE.value, str(system_id), list(_ACTIVE_JOB_STATE_VALUES)),
        )
        row = await cur.fetchone()
    recorded = None if row is None or row["error_category"] is None else row["error_category"]
    if recorded is not None and recorded != ErrorCategory.LEASE_EXPIRED.value:
        _log.info(
            "reconciler: stalled restoring system %s has a job category (%s); recording none",
            system_id,
            recorded,
        )
        return None
    return ErrorCategory.RESTORE_INCOMPLETE


async def repair_stalled_creating_snapshots(conn: AsyncConnection) -> int:
    """Recover a `creating` snapshot row whose capture job can never finish -> `failed` (ADR-0378).

    A `SNAPSHOT` worker that died (or a cancel that raced the handler) can strand a `snapshots` row
    in `creating` with no active job. Resolve it to `failed` so the failed-row recycle path can
    reclaim the name; the orphaned libvirt snapshot (if the create materialized) is reaped by a
    same-name re-create's defensive pre-delete, `systems.delete_snapshot`, or teardown's
    `delete_all`. A still-active `snapshot` job is left to the normal retry path.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sn.id FROM snapshots sn "
            "WHERE sn.state = %s "
            "  AND NOT EXISTS ( "
            "    SELECT 1 FROM jobs j "
            "    WHERE j.kind = %s "
            "      AND j.payload->>'snapshot_id' = sn.id::text "
            "      AND j.state = ANY(%s) "
            "  )",
            (SnapshotState.CREATING.value, JobKind.SNAPSHOT.value, list(_ACTIVE_JOB_STATE_VALUES)),
        )
        candidates = [row["id"] for row in await cur.fetchall()]
    recovered = 0
    for snapshot_id in candidates:
        async with conn.transaction():
            snapshot = await SNAPSHOTS.get(conn, snapshot_id)
            if snapshot is None or snapshot.state is not SnapshotState.CREATING:
                continue
            await SNAPSHOTS.update_state(conn, snapshot_id, SnapshotState.FAILED)
        recovered += 1
        _log.info("reconciler: stalled creating snapshot %s -> failed", snapshot_id)
    return recovered


async def _detach_sessions_reconciler(conn: AsyncConnection, system: System) -> None:
    """Drive every non-terminal DebugSession of ``system`` to detached (reconciler principal).

    Reuses the shared DebugSession detach transition SQL; audits under the system principal
    because the reconciler has no request context.
    """
    for session_id, old_state in await detach_system_debug_sessions(conn, system):
        await audit.record_system(
            conn,
            principal=SYSTEM_RECONCILER_PRINCIPAL,
            event=detach_audit_event(system, session_id, old_state),
        )
