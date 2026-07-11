"""Neutral read helpers for DebugSession recovery context."""

from __future__ import annotations

from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.capacity.state import DebugSessionState

# A session still holds the single-attach transport while `attach`/`live`; once `detached`
# it occupies nothing. The active set is what a recovering agent needs to end or operate.
ACTIVE_SESSION_STATES: tuple[DebugSessionState, ...] = (
    DebugSessionState.ATTACH,
    DebugSessionState.LIVE,
)

_ACTIVE_BY_RUN_SQL: LiteralString = (
    "SELECT s.id FROM debug_sessions s "
    "WHERE s.run_id = %s AND s.state IN ('attach', 'live') ORDER BY s.id"
)
_ACTIVE_BY_SYSTEM_SQL: LiteralString = (
    "SELECT s.id FROM debug_sessions s JOIN runs r ON r.id = s.run_id "
    "WHERE r.system_id = %s AND s.state IN ('attach', 'live') ORDER BY s.id"
)


async def active_session_ids_for_run(conn: AsyncConnection, run_id: UUID) -> list[str]:
    """Return the ids of `attach`/`live` debug sessions for one Run (ADR-0176)."""
    return await _active_session_ids(conn, _ACTIVE_BY_RUN_SQL, run_id)


async def active_session_ids_for_system(conn: AsyncConnection, system_id: UUID) -> list[str]:
    """Return the ids of `attach`/`live` debug sessions for any Run on one System."""
    return await _active_session_ids(conn, _ACTIVE_BY_SYSTEM_SQL, system_id)


async def _active_session_ids(
    conn: AsyncConnection, query: LiteralString, value: UUID
) -> list[str]:
    async with conn.cursor() as cur:
        await cur.execute(query, (value,))
        rows = await cur.fetchall()
    return [str(row[0]) for row in rows]
