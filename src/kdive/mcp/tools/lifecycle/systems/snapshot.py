"""Snapshot/restore `systems.*` MCP handlers (ADR-0378, #1254).

Four handlers backing `systems.snapshot`, `systems.restore`, `systems.list_snapshots`, and
`systems.delete_snapshot`. Each is invoked via `with_runtime_for_system` (which resolves the
provider runtime and enforces the required role), then refuses a non-snapshot provider with
`capability_unsupported`. Snapshot/restore/delete are worker jobs — the slow, multi-GB qcow2 I/O
stays off the server plane — so the synchronous admission here only validates and enqueues.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import SNAPSHOTS, SYSTEMS, snapshot_by_name, snapshots_for_system
from kdive.domain.capacity.state import JobState, RunState, SnapshotState, SystemState
from kdive.domain.lifecycle.records import Snapshot
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import RestorePayload, SnapshotDeletePayload, SnapshotPayload
from kdive.log import bind_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import capability_unsupported as _capability_unsupported
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools.lifecycle._recovery import iso
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.services.debug.sessions import active_session_ids_for_system

# libvirt snapshot names are agent-chosen; constrain to a shell/XML-safe charset so the name is
# injection-safe in the snapshot XML the provider renders and safe as a dedup-key component.
SNAPSHOT_NAME_MAX_LEN = 64
_NAME_RE = re.compile(rf"^[A-Za-z0-9._-]{{1,{SNAPSHOT_NAME_MAX_LEN}}}$")
# A snapshot/restore/delete job that still holds the domain (not yet terminal).
_ACTIVE_JOB_STATES: tuple[str, ...] = (JobState.QUEUED.value, JobState.RUNNING.value)
# The three domain-exclusive snapshot ops; a restore refuses while any is in flight on the System.
_SNAPSHOT_OP_KINDS = (JobKind.SNAPSHOT, JobKind.RESTORE, JobKind.DELETE_SNAPSHOT)
_NON_TERMINAL_RUN = frozenset({RunState.CREATED, RunState.RUNNING})


def _validate_name(system_id: str, name: str) -> str | ToolResponse:
    if _NAME_RE.match(name):
        return name
    return _config_error(
        system_id,
        detail=f"snapshot name must be 1..{SNAPSHOT_NAME_MAX_LEN} characters of [A-Za-z0-9._-]",
        data={"reason": "invalid_snapshot_name"},
    )


def _capability_refusal(runtime: ProviderRuntime, system_id: str) -> ToolResponse | None:
    if runtime.support.supports_snapshots:
        return None
    return _capability_unsupported(
        system_id,
        capability="snapshot",
        provider=runtime.support.component_sources.provider,
        supported=[],
    )


def _admit_snapshot_op(system_id: str, runtime: ProviderRuntime) -> UUID | ToolResponse:
    """Validate the System id and require a snapshot-capable provider; UUID or refusal."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    refusal = _capability_refusal(runtime, system_id)
    if refusal is not None:
        return refusal
    return uid


async def _active_job_by_dedup(conn: AsyncConnection, dedup_key: str) -> Job | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM jobs WHERE dedup_key = %s AND state = ANY(%s)",
            (dedup_key, list(_ACTIVE_JOB_STATES)),
        )
        row = await cur.fetchone()
    return Job.model_validate(row) if row else None


async def _has_live_run(conn: AsyncConnection, system_id: UUID) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM runs WHERE system_id = %s AND state = ANY(%s) LIMIT 1",
            (system_id, [s.value for s in _NON_TERMINAL_RUN]),
        )
        return await cur.fetchone() is not None


async def _active_snapshot_op(conn: AsyncConnection, system_id: UUID) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM jobs WHERE payload->>'system_id' = %s AND kind = ANY(%s) "
            "AND state = ANY(%s) LIMIT 1",
            (str(system_id), [k.value for k in _SNAPSHOT_OP_KINDS], list(_ACTIVE_JOB_STATES)),
        )
        return await cur.fetchone() is not None


async def _resolve_snapshot_collision(
    conn: AsyncConnection, system_id: UUID, name: str
) -> ToolResponse | None:
    """Apply the name-reuse rules; return a short-circuit envelope, or ``None`` to create fresh.

    ``available`` → reject (durable name in use); genuinely in-flight ``creating`` → replay the
    job; ``failed`` or a stale ``creating`` (its ``SNAPSHOT`` job already terminal/gone) → delete
    the stranded ledger row and fall through to create a fresh row + job (auto-reclaim).
    """
    existing = await snapshot_by_name(conn, system_id, name)
    if existing is None:
        return None
    if existing.state is SnapshotState.AVAILABLE:
        return _config_error(
            str(system_id),
            detail="snapshot name in use; call systems.delete_snapshot first",
            data={"reason": "snapshot_name_in_use", "name": name},
        )
    if existing.state is SnapshotState.CREATING:
        active = await _active_job_by_dedup(conn, f"{system_id}:snapshot:{name}")
        if active is not None:
            return job_envelope(active, "system_id", system_id)
    await SNAPSHOTS.delete(conn, existing.id)
    return None


async def _audit_op(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    system_id: UUID,
    project: str,
    tool: str,
    transition: str,
    args: dict[str, JsonValue],
) -> None:
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=tool,
            object_kind="systems",
            object_id=system_id,
            transition=transition,
            args=args,
            project=project,
        ),
    )


async def snapshot_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    runtime: ProviderRuntime,
    *,
    system_id: str,
    name: str,
    include_memory: bool,
) -> ToolResponse:
    """Admit a checkpoint on a ``READY`` System and enqueue a `snapshot` job (ADR-0378).

    A live Run does not block a snapshot — checkpointing mid-debug is the primary use. The System
    stays ``READY``; the child ``snapshots`` row drives ``creating → available|failed``.
    """
    uid = _admit_snapshot_op(system_id, runtime)
    if isinstance(uid, ToolResponse):
        return uid
    validated = _validate_name(system_id, name)
    if isinstance(validated, ToolResponse):
        return validated
    with bind_context(principal=ctx.principal):
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, uid),
        ):
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})
            collision = await _resolve_snapshot_collision(conn, uid, validated)
            if isinstance(collision, ToolResponse):
                return collision
            snapshot_id = uuid4()
            now = datetime.now(UTC)
            await SNAPSHOTS.insert(
                conn,
                Snapshot(
                    id=snapshot_id,
                    created_at=now,
                    updated_at=now,
                    principal=ctx.principal,
                    agent_session=ctx.agent_session,
                    project=system.project,
                    system_id=uid,
                    name=validated,
                    include_memory=include_memory,
                    state=SnapshotState.CREATING,
                ),
            )
            await _audit_op(
                conn,
                ctx,
                system_id=uid,
                project=system.project,
                tool="systems.snapshot",
                transition="snapshot:creating",
                args={"system_id": system_id, "name": validated, "include_memory": include_memory},
            )
            job = await queue.enqueue(
                conn,
                JobKind.SNAPSHOT,
                SnapshotPayload(
                    system_id=system_id,
                    snapshot_id=str(snapshot_id),
                    name=validated,
                    include_memory=include_memory,
                ),
                job_authorizing(ctx, system.project),
                f"{uid}:snapshot:{validated}",
                recycle_terminal=True,
                recycle_canceled=True,
            )
            return job_envelope(job, "system_id", uid)


async def restore_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    runtime: ProviderRuntime,
    *,
    system_id: str,
    name: str,
    start_paused: bool,
) -> ToolResponse:
    """Admit a revert on a ``READY`` System, fence it ``READY → RESTORING``, enqueue `restore`.

    Refuses a non-``available`` snapshot, ``start_paused`` against a disk-only snapshot, a live
    Run, an in-flight snapshot/restore/delete op, or an attached debug session — each would be
    corrupted or silently broken by the revert (ADR-0378).
    """
    uid = _admit_snapshot_op(system_id, runtime)
    if isinstance(uid, ToolResponse):
        return uid
    with bind_context(principal=ctx.principal):
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, uid),
        ):
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})
            snapshot = await snapshot_by_name(conn, uid, name)
            if snapshot is None or snapshot.state is not SnapshotState.AVAILABLE:
                return _config_error(
                    system_id,
                    detail="no available snapshot by that name; call systems.list_snapshots",
                    data={"reason": "snapshot_not_available", "name": name},
                )
            if start_paused and not snapshot.include_memory:
                return _config_error(
                    system_id,
                    detail="cannot pause-restore a disk-only snapshot: it has no saved CPU/RAM "
                    "state",
                    data={"reason": "disk_only_no_pause", "name": name},
                )
            if await _has_live_run(conn, uid):
                return _config_error(
                    system_id,
                    detail="a run is holding this System; restore discards the running guest — "
                    "wait for the run or cancel it first",
                    data={"reason": "live_run", "current_status": system.state.value},
                )
            if await _active_snapshot_op(conn, uid):
                return _config_error(
                    system_id,
                    detail="a snapshot capture/restore/delete is in progress; wait for it first",
                    data={"reason": "snapshot_op_in_progress"},
                )
            if await active_session_ids_for_system(conn, uid):
                return _config_error(
                    system_id,
                    detail="a debug session is attached; call debug.end_session first (attach a "
                    "fresh session after the restore)",
                    data={"reason": "debug_session_attached"},
                )
            await SYSTEMS.update_state(conn, uid, SystemState.RESTORING)
            await _audit_op(
                conn,
                ctx,
                system_id=uid,
                project=system.project,
                tool="systems.restore",
                transition="ready->restoring",
                args={"system_id": system_id, "name": name, "start_paused": start_paused},
            )
            job = await queue.enqueue(
                conn,
                JobKind.RESTORE,
                RestorePayload(system_id=system_id, name=name, start_paused=start_paused),
                job_authorizing(ctx, system.project),
                f"{uid}:restore:{name}:{start_paused}",
                recycle_terminal=True,
            )
            return job_envelope(job, "system_id", uid)


def _snapshot_item(snapshot: Snapshot) -> ToolResponse:
    return ToolResponse.success(
        str(snapshot.id),
        "ok",
        data={
            "name": snapshot.name,
            "state": snapshot.state.value,
            "include_memory": snapshot.include_memory,
            "system_id": str(snapshot.system_id),
            "created_at": iso(snapshot.created_at),
        },
    )


async def list_snapshots(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    runtime: ProviderRuntime,
    *,
    system_id: str,
) -> ToolResponse:
    """Return a System's snapshots newest-first from Postgres (no libvirt round-trip)."""
    uid = _admit_snapshot_op(system_id, runtime)
    if isinstance(uid, ToolResponse):
        return uid
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            rows = await snapshots_for_system(conn, uid)
    return ToolResponse.collection(
        system_id,
        "ok",
        [_snapshot_item(row) for row in rows],
        suggested_next_actions=["systems.snapshot", "systems.restore"],
        data={"system_id": system_id},
    )


async def delete_snapshot(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    runtime: ProviderRuntime,
    *,
    system_id: str,
    name: str,
) -> ToolResponse:
    """Admit a snapshot deletion and enqueue a `delete_snapshot` job (ADR-0378).

    Async because freeing an internal memory snapshot merges the same multi-GB qcow2 clusters that
    made capture a job. Refuses a ``creating`` snapshot (cancel the capture first) or a
    ``RESTORING`` System (a concurrent restore could revert the snapshot being removed). No
    System-state transition — deletion does not disturb the guest.
    """
    uid = _admit_snapshot_op(system_id, runtime)
    if isinstance(uid, ToolResponse):
        return uid
    with bind_context(principal=ctx.principal):
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, uid),
        ):
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            snapshot = await snapshot_by_name(conn, uid, name)
            if snapshot is None:
                return _config_error(
                    system_id,
                    detail="no snapshot by that name",
                    data={"reason": "snapshot_not_found", "name": name},
                )
            if snapshot.state is SnapshotState.CREATING:
                return _config_error(
                    system_id,
                    detail="snapshot is still being captured; cancel the capture job first",
                    data={"reason": "snapshot_creating", "name": name},
                )
            if system.state is SystemState.RESTORING:
                return _config_error(
                    system_id,
                    detail="a restore is in progress; wait for it before deleting a snapshot",
                    data={"reason": "system_restoring"},
                )
            await _audit_op(
                conn,
                ctx,
                system_id=uid,
                project=system.project,
                tool="systems.delete_snapshot",
                transition="delete_snapshot",
                args={"system_id": system_id, "name": name},
            )
            job = await queue.enqueue(
                conn,
                JobKind.DELETE_SNAPSHOT,
                SnapshotDeletePayload(system_id=system_id, snapshot_id=str(snapshot.id), name=name),
                job_authorizing(ctx, system.project),
                f"{uid}:delete_snapshot:{name}",
                recycle_terminal=True,
                recycle_canceled=True,
            )
            return job_envelope(job, "system_id", uid)


__all__ = ["delete_snapshot", "list_snapshots", "restore_system", "snapshot_system"]
