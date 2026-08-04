"""Direct tests for the shared debug-session context resolver."""

from __future__ import annotations

import asyncio

from kdive.domain.capacity.state import DebugSessionState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug.sessions.context import (
    debug_session_error,
    resolve_debug_session_context,
)
from tests.mcp.debug.session_support import pool, request_context, seed_live_session


def test_debug_session_error_uses_configuration_category() -> None:
    resp = debug_session_error("not-a-uuid", "bad_session_id")

    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    assert resp.data == {"code": "bad_session_id"}


def test_resolve_context_returns_live_session_and_system_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with pool(migrated_url) as db_pool:
            session_id = await seed_live_session(db_pool, state=DebugSessionState.LIVE)
            async with db_pool.connection() as conn:
                resolved = await resolve_debug_session_context(
                    conn, request_context(), session_id, require_live=True, include_system=True
                )
        assert not isinstance(resolved, ToolResponse)
        assert str(resolved.session_id) == session_id
        assert resolved.session.state is DebugSessionState.LIVE
        assert resolved.system_id is not None

    asyncio.run(_run())


def test_resolve_context_rejects_non_live_session(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with pool(migrated_url) as db_pool:
            session_id = await seed_live_session(db_pool, state=DebugSessionState.DETACHED)
            async with db_pool.connection() as conn:
                resolved = await resolve_debug_session_context(
                    conn, request_context(), session_id, require_live=True
                )
        assert isinstance(resolved, ToolResponse)
        return resolved

    resp = asyncio.run(_run())

    assert resp.status == "error"
    assert resp.data == {"code": "not_live", "current_status": "detached"}
