"""Allocation release and renew MCP handlers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import ErrorCategory
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools.lifecycle.allocations.common import allocation_next_actions
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.allocation.release import (
    ReleaseOutcome,
    ctx_audit_writer,
    release_with_backstops,
)
from kdive.services.allocation.renew import RenewOutcome, renew


async def release_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Drive an allocation to released under the service-level backstops."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _not_found(allocation_id)
            require_role(ctx, alloc.project, Role.OPERATOR)
        outcome = await release_with_backstops(
            pool, uid, project=alloc.project, audit_writer=ctx_audit_writer(ctx)
        )
        return _release_response(uid, outcome)


def _release_response(uid: UUID, outcome: ReleaseOutcome) -> ToolResponse:
    if outcome.released:
        return ToolResponse.success(str(uid), "released")
    data: dict[str, Any] = dict(outcome.details)
    if outcome.current_status:
        data["current_status"] = outcome.current_status
    category = outcome.category or ErrorCategory.CONFIGURATION_ERROR
    return ToolResponse.failure(
        str(uid),
        category,
        suggested_next_actions=["allocations.get"]
        if category is ErrorCategory.STALE_HANDLE
        else [],
        data=data,
    )


async def renew_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    allocation_id: str,
    *,
    extend: object,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Extend an allocation lease window after authorization and service validation."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _not_found(allocation_id)
            require_role(ctx, alloc.project, Role.OPERATOR)
            outcome = await renew(
                conn, ctx, allocation_id=uid, extend=extend, idempotency_key=idempotency_key
            )
        return _renew_response(uid, outcome)


def _renew_response(uid: UUID, outcome: RenewOutcome) -> ToolResponse:
    if outcome.renewed and outcome.allocation is not None:
        return ToolResponse.success(
            str(uid),
            outcome.allocation.state.value,
            suggested_next_actions=allocation_next_actions(outcome.allocation.state),
            data={"project": outcome.allocation.project},
        )
    category = outcome.category or ErrorCategory.ALLOCATION_DENIED
    data: dict[str, Any] = dict(outcome.details)
    if outcome.current_status:
        data["current_status"] = outcome.current_status
    return ToolResponse.failure(
        str(uid),
        category,
        suggested_next_actions=["allocations.get"],
        data=data,
    )
