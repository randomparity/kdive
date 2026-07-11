"""Shared platform-audited read pagination for ops audit tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import InvalidCursor
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import paginate as _paginate
from kdive.mcp.tools.ops import _reads
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    require_platform_role,
)

type AuditReadRow = dict[str, object]
type CursorAnchor = tuple[datetime, UUID]
type FetchRows = Callable[
    [AsyncConnection, int, CursorAnchor | None], Awaitable[list[AuditReadRow]]
]
type RowData = Callable[[AuditReadRow], dict[str, str]]
type RowObjectId = Callable[[AuditReadRow, dict[str, str]], str]


async def query_platform_audited_page(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    object_id: str,
    list_tag: str,
    args: dict[str, object],
    limit: int,
    cursor: str | None,
    fetch_rows: FetchRows,
    row_data: RowData,
    row_object_id: RowObjectId,
) -> ToolResponse:
    """Authorize, read-audit, and return one newest-first keyset page."""
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)
    except AuthorizationError:
        await _reads.audit_denial(pool, ctx, tool=tool, args=args)
        return ToolResponse.failure(
            object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
        )

    after: CursorAnchor | None = None
    if cursor:
        try:
            after = _decode_ts_uuid_cursor(list_tag, cursor)
        except InvalidCursor:
            async with pool.connection() as conn:
                await _reads.record_read(conn, ctx, tool=tool, args=args)
            return _invalid_cursor_error(object_id)

    capped = _clamp_list_limit(limit)
    async with pool.connection() as conn:
        rows = await fetch_rows(conn, capped, after)
        await _reads.record_read(conn, ctx, tool=tool, args=args)
    return _response(
        rows,
        capped,
        tool=tool,
        object_id=object_id,
        list_tag=list_tag,
        row_data=row_data,
        row_object_id=row_object_id,
    )


def _response(
    rows: list[AuditReadRow],
    limit: int,
    *,
    tool: str,
    object_id: str,
    list_tag: str,
    row_data: RowData,
    row_object_id: RowObjectId,
) -> ToolResponse:
    kept, truncated = _paginate(rows, limit)
    items: list[ToolResponse] = []
    for row in kept:
        data = row_data(row)
        items.append(ToolResponse.success(row_object_id(row, data), "ok", data=data))

    next_cursor: JsonValue = None
    if truncated and kept:
        last = kept[-1]
        ts, row_id = last["ts"], last["id"]
        if isinstance(ts, datetime) and isinstance(row_id, UUID):
            next_cursor = _encode_ts_uuid_cursor(list_tag, ts, row_id)

    return ToolResponse.collection(
        object_id,
        "ok",
        items,
        suggested_next_actions=[tool],
        data={"truncated": truncated, "next_cursor": next_cursor},
    )
