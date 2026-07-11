"""Read-side allocation MCP handlers."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.sql import Composable
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.capacity.state import AllocationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.log import bind_context
from kdive.mcp.exposure import tool_visible, visible_next_actions
from kdive.mcp.responses import JsonValue, ToolResponse
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
from kdive.mcp.tools.lifecycle.allocations.common import (
    MAX_WAIT_S,
    POLL_INTERVAL_S,
    envelope_for_allocation,
    queue_position,
)
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role

_log = logging.getLogger(__name__)


async def get_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Return an allocation visible to the caller, or a no-leak not_found."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _invalid_uuid_error("allocation_id", allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _not_found(allocation_id)
            require_role(ctx, alloc.project, Role.VIEWER)
            position = (
                await queue_position(conn, alloc)
                if alloc.state is AllocationState.REQUESTED
                else None
            )
        return envelope_for_allocation(alloc, ctx, queue_position=position)


async def wait_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    allocation_id: str,
    timeout_s: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ToolResponse:
    """Poll until a requested allocation settles or the clamped timeout elapses."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _invalid_uuid_error("allocation_id", allocation_id)
    if not math.isfinite(timeout_s):
        return _config_error_reason(
            allocation_id,
            ConfigErrorReason.INVALID_TIMEOUT,
            detail="timeout_s must be a finite number of seconds",
        )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(max(timeout_s, 0.0), MAX_WAIT_S)
    with bind_context(principal=ctx.principal):
        while True:
            async with pool.connection() as conn:
                alloc = await ALLOCATIONS.get(conn, uid)
                if alloc is None or alloc.project not in ctx.projects:
                    return _not_found(allocation_id)
                require_role(ctx, alloc.project, Role.VIEWER)
                position = (
                    await queue_position(conn, alloc)
                    if alloc.state is AllocationState.REQUESTED
                    else None
                )
            now = loop.time()
            if alloc.state is not AllocationState.REQUESTED or now >= deadline:
                return envelope_for_allocation(alloc, ctx, queue_position=position)
            await sleep(min(POLL_INTERVAL_S, deadline - now))


_ALLOCATIONS_LIST_TAG = "allocations.list"
_ALLOCATIONS_COLLECTION_ACTIONS = ["allocations.get", "allocations.release"]


@dataclass(frozen=True, slots=True)
class AllocationsListRequest:
    """Filter payload for ``allocations.list``."""

    project: str | None = None
    state: AllocationState | None = None
    limit: int = DEFAULT_LIST_LIMIT
    cursor: str | None = None


def _viewer_projects(ctx: RequestContext) -> list[str]:
    """Projects the caller may view: a member project with any granted role."""
    return [p for p in ctx.projects if ctx.roles.get(p) is not None]


def _project_filter(ctx: RequestContext, project: str | None) -> list[str]:
    if project is not None:
        require_project(ctx, project)
        require_role(ctx, project, Role.VIEWER)
        return [project]
    return _viewer_projects(ctx)


async def list_allocations(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: AllocationsListRequest | None = None,
) -> ToolResponse:
    """Return a page of the newest visible allocations (keyset-paginated, ADR-0192).

    Fetches one row past ``limit`` to set ``data.truncated``/``data.next_cursor`` exactly
    from the last kept allocation's ``(created_at, id)``. A ``cursor`` resumes strictly
    after a prior page; a malformed/wrong-tool cursor is an ``invalid_cursor`` config error.

    Omit ``project`` to list allocations across every project where the caller has at least
    viewer access. Supplying ``project`` preserves the stricter single-project authorization
    errors used by the previous contract.

    Optional ``state`` filter (ADR-0197) narrows by lifecycle state, applied before the
    keyset seek so the cursor stays a pure boundary and following ``next_cursor`` drains
    the full filtered set.
    """
    request = request or AllocationsListRequest()
    projects = _project_filter(ctx, request.project)
    capped = _clamp_list_limit(request.limit)
    after = None
    if request.cursor:
        try:
            after = _decode_ts_uuid_cursor(_ALLOCATIONS_LIST_TAG, request.cursor)
        except InvalidCursor:
            return _invalid_cursor_error("allocations")
    where_parts: list[Composable] = [sql.SQL("project = ANY(%s)")]
    params: list[object] = [projects]
    if request.state is not None:
        where_parts.append(sql.SQL("state = %s"))
        params.append(request.state.value)
    if after is not None:
        where_parts.append(sql.SQL("(created_at, id) < (%s, %s)"))
        params.extend(after)
    params.append(capped + 1)
    with bind_context(principal=ctx.principal):
        if not projects:
            return _allocations_collection(
                ctx, [], project=request.project, truncated=False, next_cursor=None
            )
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            query = sql.SQL(
                "SELECT * FROM allocations WHERE {where} ORDER BY created_at DESC, id DESC LIMIT %s"
            ).format(where=sql.SQL(" AND ").join(where_parts))
            await cur.execute(query, params)
            rows = await cur.fetchall()
        kept, truncated = _paginate(rows, capped)
        responses: list[ToolResponse] = []
        for row in kept:
            try:
                responses.append(envelope_for_allocation(Allocation.model_validate(row), ctx))
            except ValueError:
                _log.warning("allocation row violates the response invariant; degraded")
                responses.append(
                    ToolResponse.failure(
                        str(row.get("id", "?")), ErrorCategory.INFRASTRUCTURE_FAILURE
                    )
                )
        # The cursor boundary is the last kept *row* (raw columns), not the last
        # successfully-validated model, so a degraded trailing row never shifts the page
        # boundary and skips a healthy successor.
        next_cursor = (
            _encode_ts_uuid_cursor(_ALLOCATIONS_LIST_TAG, kept[-1]["created_at"], kept[-1]["id"])
            if truncated and kept
            else None
        )
        return _allocations_collection(
            ctx,
            responses,
            project=request.project,
            truncated=truncated,
            next_cursor=next_cursor,
        )


def _allocations_collection(
    ctx: RequestContext,
    responses: list[ToolResponse],
    *,
    project: str | None,
    truncated: bool,
    next_cursor: str | None,
) -> ToolResponse:
    actions = (
        visible_next_actions(_ALLOCATIONS_COLLECTION_ACTIONS, ctx, project)
        if project is not None
        else [name for name in _ALLOCATIONS_COLLECTION_ACTIONS if tool_visible(name, ctx)]
    )
    data: dict[str, JsonValue] = {"truncated": truncated, "next_cursor": next_cursor}
    if project is not None:
        data["project"] = project
    return ToolResponse.collection(
        "allocations",
        "ok",
        responses,
        suggested_next_actions=actions,
        data=data,
    )
