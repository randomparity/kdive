"""MCP adapters for Investigation reads."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import InvestigationState
from kdive.domain.lifecycle.records import Investigation
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, ConfigErrorReason, InvalidCursor
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import paginate as _paginate
from kdive.mcp.tools.lifecycle.investigations.common import investigation_error_response
from kdive.mcp.tools.lifecycle.investigations.view import (
    attachments_for_investigations,
    envelope_for_investigation,
    investigation_list_item,
    render_list_item,
)
from kdive.security.authz.context import RequestContext
from kdive.services.investigations.common import InvestigationServiceError
from kdive.services.investigations.read import get_investigation_record, list_investigation_rows
from kdive.services.investigations.view import InvestigationRowError

_LIST_TAG = "investigations.list"


async def get_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    """Return an Investigation the caller's project owns."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    try:
        inv = await get_investigation_record(pool, ctx, uid, raw_id=investigation_id)
    except InvestigationServiceError as exc:
        return investigation_error_response(exc)
    async with pool.connection() as conn:
        return await envelope_for_investigation(conn, inv)


def _state_filter(state: str | None) -> InvestigationState | ToolResponse | None:
    if state is None:
        return None
    try:
        return InvestigationState(state)
    except ValueError:
        return _config_error_reason(
            "investigations.list",
            ConfigErrorReason.INVALID_STATE,
            accepted_values=[s.value for s in InvestigationState],
            detail=f"state {state!r} is not a valid Investigation state",
        )


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
    resolved_state = _state_filter(state)
    if isinstance(resolved_state, ToolResponse):
        return resolved_state
    capped = _clamp_list_limit(limit)
    after = None
    if cursor:
        try:
            after = _decode_ts_uuid_cursor(_LIST_TAG, cursor)
        except InvalidCursor:
            return _invalid_cursor_error("investigations.list")
    rows = await list_investigation_rows(
        pool,
        ctx,
        project=project,
        state=resolved_state,
        limit=capped + 1,
        after=after,
    )
    kept, truncated = _paginate(rows, capped)
    next_cursor = (
        _encode_ts_uuid_cursor(_LIST_TAG, kept[-1]["created_at"], kept[-1]["id"])
        if truncated and kept
        else None
    )
    render_queue = [investigation_list_item(row) for row in kept]
    investigations = [item for item in render_queue if isinstance(item, Investigation)]
    async with pool.connection() as conn:
        attachments = await attachments_for_investigations(conn, [inv.id for inv in investigations])
    items = [
        render_list_item(item, attachments)
        for item in render_queue
        if isinstance(item, (Investigation, InvestigationRowError))
    ]
    return ToolResponse.collection(
        "investigations",
        "ok",
        items,
        suggested_next_actions=["investigations.get", "investigations.open"],
        data={"truncated": truncated, "next_cursor": next_cursor},
    )


__all__ = ["get_investigation", "list_investigations"]
