"""Debug detach service boundary tests."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

import psycopg
import pytest

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import (
    DebugSessionState,
    ExternalBootActivationState,
    SystemState,
)
from kdive.providers.ports.handles import TransportHandle
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.debug.detach import detach_audit_event, detach_system_debug_sessions
from kdive.services.debug.lifecycle import detach_locked
from kdive.services.external_boot import ExternalBootDenied
from tests.reconciler.conftest import connect, seed_debug_session, seed_run, seed_system
from tests.services.external_boot.conftest import seed_activation


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


class _InertConnector:
    """Records closes; a denied detach must not reach it."""

    def __init__(self) -> None:
        self.closed: list[str] = []

    def close_transport(self, handle: TransportHandle) -> None:
        self.closed.append(str(handle))


def test_detach_locked_is_denied_for_a_session_run_that_does_not_own_the_activation(
    migrated_url: str,
) -> None:
    """ADR-0583: ``debug_detach`` is owning-Run scoped, and the guard precedes the transition."""

    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.READY)
        owning_run_id = await seed_run(conn, system_id)
        other_run_id = await seed_run(conn, system_id)
        session_id = await seed_debug_session(
            conn, other_run_id, state=DebugSessionState.LIVE, transport_handle="handle-1"
        )
        await seed_activation(
            conn,
            state=ExternalBootActivationState.ACTIVE,
            system_id=system_id,
            run_id=owning_run_id,
        )
        connector = _InertConnector()
        ctx = RequestContext(
            principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.ADMIN}
        )

        with pytest.raises(ExternalBootDenied) as denied:
            await detach_locked(conn, ctx, session_id, system_id, cast(Any, connector))

        assert denied.value.next_actions == ["runs.get", "runs.release_external_boot"]
        assert connector.closed == []
        assert await _session_state(conn, session_id) == DebugSessionState.LIVE.value
        await conn.close()

    asyncio.run(_run())
