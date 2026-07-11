"""Read handlers for Investigation MCP tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import InvestigationState
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, ConfigErrorReason, InvalidCursor
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools._common import paginate as _paginate
from kdive.mcp.tools.lifecycle.investigations.view import (
    attachments_for_investigations,
    envelope_for_investigation,
    investigation_envelope,
    investigation_list_item,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, projects_with_role, require_role

_LIST_TAG = "investigations.list"


async def get_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    """Return an Investigation the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
            if inv is None or inv.project not in ctx.projects:
                return _not_found(investigation_id)
            require_role(ctx, inv.project, Role.VIEWER)
            return await envelope_for_investigation(conn, inv)


async def _fetch_investigation_rows(
    conn: AsyncConnection,
    projects: tuple[str, ...],
    state: InvestigationState | None,
    *,
    limit: int,
    after: tuple[datetime, UUID] | None,
) -> list[dict[str, Any]]:
    """Fetch a keyset page of raw investigation rows."""
    query = "SELECT * FROM investigations WHERE project = ANY(%s)"
    params: list[object] = [list(projects)]
    if state is not None:
        query += " AND state = %s"
        params.append(state.value)
    if after is not None:
        query += " AND (created_at, id) < (%s, %s)"
        params.extend(after)
    query += " ORDER BY created_at DESC, id DESC LIMIT %s"
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


async def list_investigations(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str | None = None,
    state: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> ToolResponse:
    """List the caller's viewer-project Investigations, newest-first."""
    resolved_state: InvestigationState | None = None
    if state is not None:
        try:
            resolved_state = InvestigationState(state)
        except ValueError:
            return _config_error_reason(
                "investigations.list",
                ConfigErrorReason.INVALID_STATE,
                accepted_values=[s.value for s in InvestigationState],
                detail=f"state {state!r} is not a valid Investigation state",
            )
    capped = _clamp_list_limit(limit)
    after = None
    if cursor:
        try:
            after = _decode_ts_uuid_cursor(_LIST_TAG, cursor)
        except InvalidCursor:
            return _invalid_cursor_error("investigations.list")
    with bind_context(principal=ctx.principal):
        viewer_projects = tuple(projects_with_role(ctx, Role.VIEWER))
        if project is not None:
            viewer_projects = tuple(p for p in viewer_projects if p == project)
        async with pool.connection() as conn:
            rows = await _fetch_investigation_rows(
                conn, viewer_projects, resolved_state, limit=capped + 1, after=after
            )
            kept, truncated = _paginate(rows, capped)
            next_cursor = (
                _encode_ts_uuid_cursor(_LIST_TAG, kept[-1]["created_at"], kept[-1]["id"])
                if truncated and kept
                else None
            )
            render_queue = [investigation_list_item(row) for row in kept]
            investigations = [item for item in render_queue if not isinstance(item, ToolResponse)]
            attachments = await attachments_for_investigations(
                conn, [inv.id for inv in investigations]
            )
            items = [
                item
                if isinstance(item, ToolResponse)
                else investigation_envelope(item, attachments[item.id])
                for item in render_queue
            ]
        return ToolResponse.collection(
            "investigations",
            "ok",
            items,
            suggested_next_actions=["investigations.get", "investigations.open"],
            data={"truncated": truncated, "next_cursor": next_cursor},
        )
