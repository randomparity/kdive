"""Debug detach service boundary tests."""

from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import DebugSessionState, SystemState
from kdive.services.debug.detach import detach_audit_event, detach_system_debug_sessions
from tests.reconciler.conftest import connect, seed_debug_session, seed_run, seed_system


async def _session_state(conn: psycopg.AsyncConnection, session_id: UUID) -> str:
    cur = await conn.execute("SELECT state FROM debug_sessions WHERE id = %s", (session_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


def test_detach_system_debug_sessions_detaches_only_active_sessions(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.READY)
        run_id = await seed_run(conn, system_id)
        live_id = await seed_debug_session(conn, run_id, state=DebugSessionState.LIVE)
        attach_id = await seed_debug_session(conn, run_id, state=DebugSessionState.ATTACH)
        detached_id = await seed_debug_session(conn, run_id, state=DebugSessionState.DETACHED)
        other_system_id = await seed_system(conn, system_state=SystemState.READY)
        other_run_id = await seed_run(conn, other_system_id)
        other_live_id = await seed_debug_session(conn, other_run_id, state=DebugSessionState.LIVE)
        system = await SYSTEMS.get(conn, system_id)
        assert system is not None

        detached = await detach_system_debug_sessions(conn, system)

        assert dict(detached) == {
            live_id: DebugSessionState.LIVE.value,
            attach_id: DebugSessionState.ATTACH.value,
        }
        assert await _session_state(conn, live_id) == DebugSessionState.DETACHED.value
        assert await _session_state(conn, attach_id) == DebugSessionState.DETACHED.value
        assert await _session_state(conn, detached_id) == DebugSessionState.DETACHED.value
        assert await _session_state(conn, other_live_id) == DebugSessionState.LIVE.value
        await conn.close()

    asyncio.run(_run())


def test_detach_system_debug_sessions_returns_empty_for_terminal_sessions(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.READY)
        run_id = await seed_run(conn, system_id)
        await seed_debug_session(conn, run_id, state=DebugSessionState.DETACHED)
        system = await SYSTEMS.get(conn, system_id)
        assert system is not None

        detached = await detach_system_debug_sessions(conn, system)

        assert detached == []
        await conn.close()

    asyncio.run(_run())


def test_detach_audit_event_captures_force_crash_transition(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.CRASHING)
        run_id = await seed_run(conn, system_id)
        session_id = await seed_debug_session(conn, run_id, state=DebugSessionState.LIVE)
        system = await SYSTEMS.get(conn, system_id)
        assert system is not None

        event = detach_audit_event(system, session_id, DebugSessionState.LIVE.value)

        assert event.tool == "control.force_crash"
        assert event.object_kind == "debug_sessions"
        assert event.object_id == session_id
        assert event.transition == "live->detached"
        assert event.args == {"system_id": str(system_id)}
        assert event.project == "proj"
        await conn.close()

    asyncio.run(_run())
