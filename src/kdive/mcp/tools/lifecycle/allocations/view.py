"""Read-side allocation MCP handlers."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation
from kdive.domain.state import AllocationState
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import not_found as _not_found
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
        return _config_error(allocation_id)
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
        return envelope_for_allocation(alloc, queue_position=position)


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
        return _config_error(allocation_id)
    if not math.isfinite(timeout_s):
        return _config_error(allocation_id)
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
                return envelope_for_allocation(alloc, queue_position=position)
            await sleep(min(POLL_INTERVAL_S, deadline - now))


async def list_allocations(
    pool: AsyncConnectionPool, ctx: RequestContext, *, project: str, limit: int
) -> ToolResponse:
    """Return the newest allocations for a project in one collection envelope."""
    require_project(ctx, project)
    require_role(ctx, project, Role.VIEWER)
    capped = _clamp_list_limit(limit)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM allocations WHERE project = %s "
                "ORDER BY created_at DESC, id LIMIT %s",
                (project, capped),
            )
            rows = await cur.fetchall()
        responses: list[ToolResponse] = []
        for row in rows:
            try:
                responses.append(envelope_for_allocation(Allocation.model_validate(row)))
            except ValueError:
                _log.warning("allocation row violates the response invariant; degraded")
                responses.append(
                    ToolResponse.failure(
                        str(row.get("id", "?")), ErrorCategory.INFRASTRUCTURE_FAILURE
                    )
                )
        return ToolResponse.collection(
            "allocations",
            "ok",
            responses,
            suggested_next_actions=["allocations.get", "allocations.release"],
            data={"project": project},
        )
