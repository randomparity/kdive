"""`ops.tool_trail` platform read-tool tests (#1010, ADR-0304).

The handler is called directly with an injected pool + RequestContext (the repo's unit
contract). Coverage maps to the #1010 acceptance:

* an auditor retrieves one agent session's ordered `(tool, outcome, args_digest, ts)`
  trail, newest-first; the read writes exactly one ``platform_audit_log`` row.
* ``platform_admin`` satisfies the auditor gate; a project-only token is denied and the
  denial is not audited (routine non-grant on an openly-callable read).
* filters (``principal`` / ``tool``) narrow the rows; the window defaults to the last 24h
  (an older row is excluded unless an explicit start reaches back for it).
* keyset pagination drains the full set; a malformed window fails closed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.ops.audit import tool_trail as trail_tools
from kdive.security.authz.rbac import PlatformRole, Role

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_BASE_CTX = RequestContext(principal="user-1", agent_session="sess-1", projects=())


def _platform_ctx(role: PlatformRole) -> RequestContext:
    return replace(_BASE_CTX, platform_roles=frozenset({role}))


def _project_ctx() -> RequestContext:
    return replace(_BASE_CTX, projects=("proj-a",), roles={"proj-a": Role.ADMIN})


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _row(
    conn: psycopg.AsyncConnection,
    *,
    principal: str,
    agent_session: str | None,
    tool: str,
    outcome: str,
    ts: datetime,
    args_digest: str,
    project: str | None = None,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO tool_invocation "
            "(principal, agent_session, project, tool, outcome, actor, client_id, args_digest, ts) "
            "VALUES (%s, %s, %s, %s, %s, 'agent', NULL, %s, %s)",
            (principal, agent_session, project, tool, outcome, args_digest, ts),
        )


async def _seed_session(pool: AsyncConnectionPool) -> None:
    """Seed sess-1 with three ordered calls plus a distractor row in another session."""
    async with pool.connection() as conn, conn.transaction():
        await _row(
            conn,
            principal="alice",
            agent_session="sess-1",
            tool="runs.install",
            outcome="ok",
            ts=_NOW - timedelta(minutes=30),
            args_digest="d1",
        )
        await _row(
            conn,
            principal="alice",
            agent_session="sess-1",
            tool="systems.authorize_ssh_key",
            outcome="error",
            ts=_NOW - timedelta(minutes=20),
            args_digest="d2",
        )
        await _row(
            conn,
            principal="alice",
            agent_session="sess-1",
            tool="jobs.wait",
            outcome="ok",
            ts=_NOW - timedelta(minutes=10),
            args_digest="d3",
        )
        await _row(
            conn,
            principal="bob",
            agent_session="sess-2",
            tool="runs.install",
            outcome="ok",
            ts=_NOW - timedelta(minutes=5),
            args_digest="dX",
        )


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT principal, platform_role, tool, scope FROM platform_audit_log")
        return list(await cur.fetchall())


def _rows(resp: ToolResponse) -> list[dict[str, object]]:
    return [cast(dict[str, object], item.data) for item in resp.items]


def _query(
    *,
    agent_session: str | None = None,
    principal: str | None = None,
    tool: str | None = None,
    window: list[str | None] | None = None,
    limit: int = trail_tools.DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> trail_tools.ToolTrailQuery:
    return trail_tools.ToolTrailQuery(
        agent_session=agent_session,
        principal=principal,
        tool=tool,
        window=window,
        limit=limit,
        cursor=cursor,
    )


def test_auditor_reads_session_trail_newest_first_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_session(pool)
            ctx = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            resp = await trail_tools.tool_trail(
                pool, ctx, request=_query(agent_session="sess-1"), now=_NOW
            )
        assert resp.status == "ok"
        rows = _rows(resp)
        # Only sess-1 rows, newest-first, carrying the (tool, outcome, args_digest, ts) trail.
        assert [(r["tool"], r["outcome"], r["args_digest"]) for r in rows] == [
            ("jobs.wait", "ok", "d3"),
            ("systems.authorize_ssh_key", "error", "d2"),
            ("runs.install", "ok", "d1"),
        ]
        assert all(r["agent_session"] == "sess-1" for r in rows)
        # Exactly one platform_audit_log row records the cross-tenant read.
        assert await _platform_audit_rows(migrated_url) == [
            ("user-1", "platform_auditor", "ops.tool_trail", "all-projects")
        ]

    asyncio.run(_run())


def test_platform_admin_satisfies_gate(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_session(pool)
            ctx = _platform_ctx(PlatformRole.PLATFORM_ADMIN)
            resp = await trail_tools.tool_trail(
                pool, ctx, request=_query(agent_session="sess-1"), now=_NOW
            )
        assert resp.status == "ok"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_admin"

    asyncio.run(_run())


def test_project_only_token_denied_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_session(pool)
            resp = await trail_tools.tool_trail(
                pool, _project_ctx(), request=_query(agent_session="sess-1"), now=_NOW
            )
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        # A project-only token's denial is the routine non-grant case → not audited.
        assert await _platform_audit_rows(migrated_url) == []

    asyncio.run(_run())


def test_principal_and_tool_filters_narrow_rows(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_session(pool)
            ctx = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            by_principal = await trail_tools.tool_trail(
                pool, ctx, request=_query(principal="bob"), now=_NOW
            )
            by_tool = await trail_tools.tool_trail(
                pool, ctx, request=_query(tool="runs.install"), now=_NOW
            )
        assert {r["agent_session"] for r in _rows(by_principal)} == {"sess-2"}
        assert {r["tool"] for r in _rows(by_tool)} == {"runs.install"}
        assert len(_rows(by_tool)) == 2  # alice's + bob's runs.install

    asyncio.run(_run())


def test_default_window_bounds_to_last_24h(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn, conn.transaction():
                await _row(
                    conn,
                    principal="alice",
                    agent_session="sess-1",
                    tool="runs.install",
                    outcome="ok",
                    ts=_NOW - timedelta(hours=48),
                    args_digest="old",
                )
                await _row(
                    conn,
                    principal="alice",
                    agent_session="sess-1",
                    tool="jobs.wait",
                    outcome="ok",
                    ts=_NOW - timedelta(hours=1),
                    args_digest="recent",
                )
            ctx = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            default = await trail_tools.tool_trail(
                pool, ctx, request=_query(agent_session="sess-1"), now=_NOW
            )
            explicit = await trail_tools.tool_trail(
                pool,
                ctx,
                request=_query(
                    agent_session="sess-1",
                    window=[(_NOW - timedelta(hours=72)).isoformat(), None],
                ),
                now=_NOW,
            )
        # Default read excludes the 48h-old row; an explicit reaching-back start includes it.
        assert {r["args_digest"] for r in _rows(default)} == {"recent"}
        assert {r["args_digest"] for r in _rows(explicit)} == {"old", "recent"}

    asyncio.run(_run())


def test_keyset_pagination_drains_the_set(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_session(pool)
            ctx = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            first = await trail_tools.tool_trail(
                pool, ctx, request=_query(agent_session="sess-1", limit=2), now=_NOW
            )
            assert first.data["truncated"] is True
            cursor = cast(str, first.data["next_cursor"])
            second = await trail_tools.tool_trail(
                pool,
                ctx,
                request=_query(agent_session="sess-1", limit=2, cursor=cursor),
                now=_NOW,
            )
        assert [r["args_digest"] for r in _rows(first)] == ["d3", "d2"]
        assert [r["args_digest"] for r in _rows(second)] == ["d1"]
        assert second.data["truncated"] is False
        assert second.data["next_cursor"] is None

    asyncio.run(_run())


def test_malformed_window_fails_closed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            resp = await trail_tools.tool_trail(
                pool, ctx, request=_query(window=["not-a-timestamp", None]), now=_NOW
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_invalid_cursor_fails_closed_but_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_session(pool)
            ctx = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            resp = await trail_tools.tool_trail(
                pool,
                ctx,
                request=_query(agent_session="sess-1", cursor="not-a-cursor"),
                now=_NOW,
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        # The read reached the gate and was authorized, so the access is recorded.
        assert len(await _platform_audit_rows(migrated_url)) == 1

    asyncio.run(_run())
