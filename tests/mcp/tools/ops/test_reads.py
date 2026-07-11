"""Cover the shared cross-project platform-auditor read helpers (ADR-0062 §6)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.platform_auth import ALL_PROJECTS_SCOPE
from kdive.mcp.tools.ops import _reads
from kdive.security.authz.context import RequestContext


def test_parse_window_delegates_with_audit_log_timestamp_column(monkeypatch) -> None:
    captured: list[tuple[Any, str]] = []

    def _parse(window: Any, *, timestamp_column: str) -> str:
        captured.append((window, timestamp_column))
        return "parsed"

    monkeypatch.setattr(_reads, "parse_timestamptz_window", _parse)
    assert _reads.parse_window(["a", "b"]) == "parsed"
    assert captured == [(["a", "b"], "audit_log.ts")]


class _Conn:
    def transaction(self) -> Any:
        @asynccontextmanager
        async def _txn() -> Any:
            yield None

        return _txn()


def _ctx() -> RequestContext:
    return cast("RequestContext", SimpleNamespace(principal="alice", agent_session="sess-1"))


def test_record_read_writes_platform_audit_row(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    async def _record_platform(conn: Any, **kwargs: Any) -> None:
        calls.append({"conn": conn, **kwargs})

    role_ctxs: list[Any] = []
    actor_ctxs: list[Any] = []
    monkeypatch.setattr(_reads.audit, "record_platform", _record_platform)
    monkeypatch.setattr(
        _reads, "held_platform_roles", lambda c: role_ctxs.append(c) or ["platform_auditor"]
    )
    monkeypatch.setattr(_reads, "actor_for", lambda c: actor_ctxs.append(c) or "actor-1")

    conn = cast("AsyncConnection", _Conn())
    ctx = _ctx()
    asyncio.run(_reads.record_read(conn, ctx, tool="audit.query", args={"a": 1}))

    assert role_ctxs == [ctx]  # held_platform_roles received the real ctx, not None
    assert actor_ctxs == [ctx]  # actor_for received the real ctx, not None

    (call,) = calls
    assert call["conn"] is conn
    assert call["principal"] == "alice"
    assert call["agent_session"] == "sess-1"
    event = call["event"]
    assert event.tool == "audit.query"
    assert event.scope == ALL_PROJECTS_SCOPE
    assert event.args == {"a": 1}
    assert event.platform_role == ["platform_auditor"]
    assert event.actor == "actor-1"


def test_audit_denial_delegates_with_all_projects_scope(monkeypatch) -> None:
    calls: list[tuple[Any, Any, dict[str, Any]]] = []

    async def _denial(pool: Any, ctx: Any, **kwargs: Any) -> None:
        calls.append((pool, ctx, kwargs))

    monkeypatch.setattr(_reads, "audit_platform_denial", _denial)
    pool = cast("AsyncConnectionPool", object())
    ctx = _ctx()
    asyncio.run(_reads.audit_denial(pool, ctx, tool="inventory.list", args={"x": 2}))

    (called_pool, called_ctx, kwargs) = calls[0]
    assert called_pool is pool
    assert called_ctx is ctx
    assert kwargs == {"tool": "inventory.list", "scope": ALL_PROJECTS_SCOPE, "args": {"x": 2}}
