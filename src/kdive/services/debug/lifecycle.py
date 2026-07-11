"""Transport-neutral debug session lifecycle persistence."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import DEBUG_SESSIONS, SYSTEMS
from kdive.domain.capacity.state import DebugSessionState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import DebugSession, Run, System
from kdive.providers.ports.handles import TransportHandle
from kdive.providers.ports.lifecycle import Connector, DebugTransportKind
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.serialization import JsonValue

_log = logging.getLogger("kdive.services.debug.lifecycle")

_OCCUPIED_SQL: LiteralString = (
    "SELECT 1 FROM debug_sessions s "
    "JOIN runs r ON r.id = s.run_id "
    "WHERE r.system_id = %s AND s.transport = %s AND s.state = ANY(%s) LIMIT 1"
)
_OCCUPIED_STATES: tuple[str, ...] = (
    DebugSessionState.ATTACH.value,
    DebugSessionState.LIVE.value,
)


@dataclass(frozen=True, slots=True)
class DebugSessionRejected:
    """Transport-neutral lifecycle rejection for MCP rendering."""

    object_id: str
    category: ErrorCategory
    detail: str | None = None
    suggested_next_actions: list[str] = field(default_factory=list)
    data: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AttachRequest:
    """A prepared attach whose provider transport has not been persisted yet."""

    run: Run
    system: System
    session_id: UUID
    transport: DebugTransportKind
    connector: Connector
    missing_debuginfo: dict[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class AttachAdmitted:
    """A persisted live DebugSession."""

    session_id: UUID
    project: str
    missing_debuginfo: dict[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class DetachedSession:
    """A detached DebugSession."""

    session_id: UUID
    project: str


async def system_occupied(
    conn: AsyncConnection, system_id: UUID, transport: DebugTransportKind
) -> bool:
    """Return whether ``system_id`` already has an active session for ``transport``."""
    async with conn.cursor() as cur:
        await cur.execute(_OCCUPIED_SQL, (system_id, transport, list(_OCCUPIED_STATES)))
        return await cur.fetchone() is not None


async def insert_session_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    request: AttachRequest,
    handle: TransportHandle,
) -> AttachAdmitted | DebugSessionRejected:
    """Re-check conflict + ready under the per-System lock, then insert + drive `-> live`."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, request.system.id):
        current = await SYSTEMS.get(conn, request.system.id)
        if current is None or current.state is not SystemState.READY:
            await close_transport(request.connector, str(handle))
            status = current.state.value if current else "torn_down"
            return DebugSessionRejected(
                object_id=str(request.run.id),
                category=ErrorCategory.CONFIGURATION_ERROR,
                data={"current_status": status},
            )
        if await system_occupied(conn, request.system.id, request.transport):
            await close_transport(request.connector, str(handle))
            return DebugSessionRejected(
                object_id=str(request.run.id), category=ErrorCategory.TRANSPORT_CONFLICT
            )
        now = datetime.now(UTC)
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=request.session_id,
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=request.run.project,
                run_id=request.run.id,
                state=DebugSessionState.ATTACH,
                transport=request.transport,
                transport_handle=str(handle),
                worker_heartbeat_at=now,
            ),
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="debug.start_session",
                object_kind="debug_sessions",
                object_id=session.id,
                transition="->attach",
                args={"run_id": str(request.run.id)},
                project=request.run.project,
            ),
        )
        await DEBUG_SESSIONS.update_state(conn, session.id, DebugSessionState.LIVE)
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="debug.start_session",
                object_kind="debug_sessions",
                object_id=session.id,
                transition="attach->live",
                args={"run_id": str(request.run.id)},
                project=request.run.project,
            ),
        )
    return AttachAdmitted(
        session_id=session.id,
        project=request.run.project,
        missing_debuginfo=request.missing_debuginfo,
    )


async def detach_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    session_id: UUID,
    system_id: UUID,
    connector: Connector,
) -> DetachedSession | DebugSessionRejected:
    """Detach one live/attach DebugSession under the per-System lock."""
    select_q: LiteralString = (
        "SELECT state, transport_handle, project FROM debug_sessions WHERE id = %s FOR UPDATE"
    )
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(select_q, (session_id,))
            row = await cur.fetchone()
        if row is None:
            return DebugSessionRejected(
                object_id=str(session_id), category=ErrorCategory.CONFIGURATION_ERROR
            )
        try:
            state = DebugSessionState(row["state"])
        except ValueError as exc:
            raise CategorizedError(
                f"debug session has an unrecognized state {row['state']!r}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"session_id": str(session_id)},
            ) from exc
        if state is DebugSessionState.DETACHED:
            return DetachedSession(session_id=session_id, project=row["project"])
        await close_transport(connector, row["transport_handle"])
        await DEBUG_SESSIONS.update_state(conn, session_id, DebugSessionState.DETACHED)
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="debug.end_session",
                object_kind="debug_sessions",
                object_id=session_id,
                transition=f"{row['state']}->detached",
                args={"session_id": str(session_id)},
                project=row["project"],
            ),
        )
    return DetachedSession(session_id=session_id, project=row["project"])


async def close_transport(connector: Connector, handle: str | None) -> None:
    """Close the transport best-effort; a missing/failing close never blocks detach."""
    if handle is None:
        return
    try:
        await asyncio.to_thread(connector.close_transport, TransportHandle(handle))
    except Exception:
        _log.warning(
            "debug transport close failed; continuing detach",
            extra={"handle": handle},
            exc_info=True,
        )
