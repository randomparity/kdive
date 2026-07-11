"""The ``ops.tool_trail`` platform read tool (ADR-0304, #1010).

Pages the ``tool_invocation`` analytics trail so a platform auditor can reconstruct an
agent session's ordered tool-call sequence — the `(tool, outcome, args_digest, ts)` a
post-hoc failure analysis needs. Cross-tenant per-call data, so it takes the same
``platform_auditor`` gate and ``platform_audit_log`` read-access record as the
cross-project ``audit.query`` (ADR-0062 §6): a denial is audited only when the caller
holds ≥1 platform role (ADR-0043 §4).

Filterable by ``agent_session`` / ``principal`` / ``tool`` / time ``window``, keyset
paginated newest-first on ``(ts, id)`` (ADR-0192). The table grows with all traffic
(``jobs.wait`` polling included), so an absent window start defaults to
``now - DEFAULT_TRAIL_WINDOW`` — a default call never scans the whole table. A thin
FastMCP wrapper over a plain async handler taking the pool + request context (tested
directly, never through MCP).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, LiteralString
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.platform_auth import ALL_PROJECTS_SCOPE
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, InvalidCursor
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import paginate as _paginate
from kdive.mcp.tools._time_window import parse_timestamptz_window
from kdive.mcp.tools.ops import _reads
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    require_platform_role,
)

_TOOL = "ops.tool_trail"
_OBJECT_ID = "ops.tool_trail"
_LIST_TAG = "ops.tool_trail"

#: Absent-window-start default: bound a default trail read to the recent past so it never
#: scans the whole (all-traffic) table. An explicit start overrides it.
DEFAULT_TRAIL_WINDOW = timedelta(hours=24)


class ToolTrailQuery(ToolPayload):
    """``ops.tool_trail`` row filters."""

    agent_session: Annotated[
        str | None, Field(description="Filter to one agent session's calls.")
    ] = None
    principal: Annotated[str | None, Field(description="Filter by acting principal.")] = None
    tool: Annotated[str | None, Field(description="Filter by tool name (e.g. 'runs.create').")] = (
        None
    )
    window: Annotated[
        list[str | None] | None,
        Field(
            description=(
                "[start, end] ISO-8601 timestamptz pair; omit start to default to the last 24h."
            )
        ),
    ] = None
    limit: Annotated[
        int, Field(description=f"Maximum rows returned (capped at {MAX_LIST_LIMIT}).")
    ] = DEFAULT_LIST_LIMIT
    cursor: Annotated[
        str | None,
        Field(description="Opaque continuation cursor from a prior page's next_cursor."),
    ] = None


class _Filters:
    """The validated row filters, ready to bind into the SQL."""

    __slots__ = ("agent_session", "principal", "tool", "window")

    def __init__(
        self,
        agent_session: str | None,
        principal: str | None,
        tool: str | None,
        window: tuple[datetime | None, datetime | None] | None,
    ) -> None:
        self.agent_session = agent_session
        self.principal = principal
        self.tool = tool
        self.window = window


def _parse_filters(request: ToolTrailQuery, *, now: datetime) -> _Filters:
    """Validate filters and apply the default window start (fail-closed on a bad window).

    Raises:
        CategorizedError: ``window`` is malformed, tz-naive, or inverted.
    """
    window = parse_timestamptz_window(request.window, timestamp_column="tool_invocation.ts")
    start, end = window if window is not None else (None, None)
    if start is None:
        start = now - DEFAULT_TRAIL_WINDOW
    return _Filters(request.agent_session, request.principal, request.tool, (start, end))


async def tool_trail(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    request: ToolTrailQuery,
    now: datetime | None = None,
) -> ToolResponse:
    """Page the ``tool_invocation`` trail; requires platform auditor, read-audited.

    Returns the most recent matching rows, newest first, keyset-paginated (ADR-0192).
    ``now`` is injectable for a deterministic default-window boundary.
    """
    now = now or datetime.now(UTC)
    with bind_context(principal=ctx.principal):
        try:
            filters = _parse_filters(request, now=now)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(_OBJECT_ID, exc, suggested_next_actions=[_TOOL])
        return await _query(pool, ctx, filters, request.limit, request.cursor)


async def _query(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    filters: _Filters,
    limit: int,
    cursor: str | None,
) -> ToolResponse:
    """Auditor-gate, read-audit, then read a keyset page (mirrors ``audit.query``)."""
    args = _audit_args(filters)
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)
    except AuthorizationError:
        await _reads.audit_denial(pool, ctx, tool=_TOOL, args=args)
        return ToolResponse.failure(
            _OBJECT_ID, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL]
        )
    # Decode the cursor only after authz + read-audit so a bad cursor cannot change either.
    after: tuple[datetime, UUID] | None = None
    if cursor:
        try:
            after = _decode_ts_uuid_cursor(_LIST_TAG, cursor)
        except InvalidCursor:
            async with pool.connection() as conn:
                await _reads.record_read(conn, ctx, tool=_TOOL, args=args)
            return _invalid_cursor_error(_OBJECT_ID)
    capped = _clamp_list_limit(limit)
    async with pool.connection() as conn:
        rows = await _fetch_rows(conn, filters=filters, limit=capped, after=after)
        await _reads.record_read(conn, ctx, tool=_TOOL, args=args)
    return _response(rows, capped)


async def _fetch_rows(
    conn: AsyncConnection,
    *,
    filters: _Filters,
    limit: int,
    after: tuple[datetime, UUID] | None,
) -> list[dict[str, object]]:
    """Read filtered ``tool_invocation`` rows, ``limit + 1`` for truncation (ADR-0192).

    The WHERE clause is assembled from a fixed set of **literal** fragments (so the query
    stays a ``LiteralString`` — no runtime-string interpolation reaches the SQL); every
    filter value is bound as a ``%s`` parameter.
    """
    params: list[object] = []
    where: LiteralString = ""
    if filters.agent_session is not None:
        where += " AND agent_session = %s"
        params.append(filters.agent_session)
    if filters.principal is not None:
        where += " AND principal = %s"
        params.append(filters.principal)
    if filters.tool is not None:
        where += " AND tool = %s"
        params.append(filters.tool)
    if filters.window is not None:
        start, end = filters.window
        if start is not None:
            where += " AND ts >= %s"
            params.append(start)
        if end is not None:
            where += " AND ts < %s"
            params.append(end)
    seek: LiteralString = ""
    if after is not None:
        seek = " AND (ts, id) < (%s, %s)"
        params.extend(after)
    query: LiteralString = (
        "SELECT id, ts, principal, agent_session, project, tool, outcome, actor, "
        "client_id, args_digest FROM tool_invocation WHERE true"
        + where
        + seek
        + " ORDER BY ts DESC, id DESC LIMIT %s"
    )
    params.append(limit + 1)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


def _audit_args(filters: _Filters) -> dict[str, object]:
    """The public filter args for the read-audit record (no secret values)."""
    window = filters.window
    return {
        "scope": ALL_PROJECTS_SCOPE,
        "agent_session": filters.agent_session,
        "principal": filters.principal,
        "tool": filters.tool,
        "window": [w.isoformat() if w else None for w in window] if window else None,
    }


def _row_data(row: dict[str, object]) -> dict[str, str]:
    ts = row["ts"]
    return {
        "ts": ts.isoformat() if isinstance(ts, datetime) else str(ts),
        "principal": _as_str(row["principal"]),
        "agent_session": _as_str(row["agent_session"]),
        "project": _as_str(row["project"]),
        "tool": _as_str(row["tool"]),
        "outcome": _as_str(row["outcome"]),
        "actor": _as_str(row["actor"]),
        "client_id": _as_str(row["client_id"]),
        "args_digest": _as_str(row["args_digest"]),
    }


def _as_str(value: object) -> str:
    return "" if value is None else str(value)


def _response(rows: list[dict[str, object]], limit: int) -> ToolResponse:
    kept, truncated = _paginate(rows, limit)
    items: list[ToolResponse] = []
    for row in kept:
        data = _row_data(row)
        row_id = row["id"]
        items.append(
            ToolResponse.success(str(row_id) if row_id is not None else _OBJECT_ID, "ok", data=data)
        )
    next_cursor: JsonValue = None
    if truncated and kept:
        last = kept[-1]
        ts, row_id = last["ts"], last["id"]
        if isinstance(ts, datetime) and isinstance(row_id, UUID):
            next_cursor = _encode_ts_uuid_cursor(_LIST_TAG, ts, row_id)
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=[_TOOL],
        data={"truncated": truncated, "next_cursor": next_cursor},
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``ops.tool_trail`` tool on ``app``, bound to ``pool``."""

    @app.tool(
        name=_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def ops_tool_trail(
        request: Annotated[
            ToolTrailQuery | None,
            Field(description="Trail filters (agent_session / principal / tool / window)."),
        ] = None,
    ) -> ToolResponse:
        """Page the per-call tool-invocation trail. Requires platform auditor.

        Reconstructs an agent session's ordered tool calls — each row carries ``tool``,
        ``outcome``, ``args_digest``, and ``ts``. Returns the most recent matching rows,
        newest first, keyset-paginated: when ``data.truncated`` is ``true``, pass
        ``data.next_cursor`` back as ``request.cursor`` for the next page. Omitting the window
        start bounds the read to the last 24h; that default lower bound is relative to the
        call time, so for an exhaustive read that pages near the 24h edge, pass an explicit
        window to pin both bounds.
        """
        return await tool_trail(
            pool,
            current_context(),
            request=request or ToolTrailQuery(),
        )
