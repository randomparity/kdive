"""DebugSession detach transitions shared by control jobs and reconciler repair."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.capacity.state import DebugSessionState
from kdive.domain.lifecycle.records import System
from kdive.security import audit


async def detach_system_debug_sessions(
    conn: AsyncConnection, system: System
) -> list[tuple[UUID, str]]:
    """Drive every non-terminal DebugSession of ``system`` to detached.

    Returns ``(session_id, old_state)`` rows. The transition SQL is shared; callers record audit
    events under their own principal or job context.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "WITH targets AS ("
            "    SELECT id, state FROM debug_sessions "
            "    WHERE state IN (%s, %s) "
            "      AND run_id IN (SELECT id FROM runs WHERE system_id = %s) "
            "    FOR UPDATE"
            ") "
            "UPDATE debug_sessions s SET state = %s "
            "FROM targets t WHERE s.id = t.id "
            "RETURNING s.id, t.state",
            (
                DebugSessionState.ATTACH.value,
                DebugSessionState.LIVE.value,
                system.id,
                DebugSessionState.DETACHED.value,
            ),
        )
        return await cur.fetchall()


def detach_audit_event(system: System, session_id: UUID, old_state: str) -> audit.AuditEvent:
    """Build the ``<old_state>->detached`` audit event for a force-crash detach."""
    return audit.AuditEvent(
        tool="control.force_crash",
        object_kind="debug_sessions",
        object_id=session_id,
        transition=f"{old_state}->detached",
        args={"system_id": str(system.id)},
        project=system.project,
    )
