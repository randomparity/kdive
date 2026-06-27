"""Shared provider runtime resolution helpers for MCP tool wrappers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid, config_error
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

type RuntimeCallback = Callable[[ProviderRuntime], Awaitable[ToolResponse]]
type RuntimeResolver = Callable[
    [ProviderResolver, AsyncConnection, UUID], Awaitable[ProviderRuntime]
]


@dataclass(frozen=True, slots=True)
class _RuntimeLookup:
    object_kind: str
    sql: LiteralString
    required_role: Role
    runtime_resolver: RuntimeResolver | None = None
    requires_bound_run: bool = False


_AUTHORIZED_ALLOCATION_KIND: LiteralString = (
    "SELECT a.project, r.kind AS kind FROM allocations a "
    "LEFT JOIN resources r ON r.id = a.resource_id "
    "WHERE a.id = %s"
)
_AUTHORIZED_SYSTEM_KIND: LiteralString = (
    "SELECT s.project, r.kind AS kind FROM systems s "
    "JOIN allocations a ON a.id = s.allocation_id "
    "JOIN resources r ON r.id = a.resource_id "
    "WHERE s.id = %s"
)
# A target-kind-only Run lookup is authorized by the Run row and selected by its committed
# ``target_kind``. It deliberately does not join System/Allocation/Resource, so unbound Runs
# (ADR-0169, ``system_id IS NULL``) resolve for ``runs.build`` / ``runs.complete_build`` before
# any System exists. Bound-run operations use ``ProviderResolver.runtime_for_run`` instead, which
# applies ``ProviderRuntime.for_resource(name)`` before capability or port handoff.
_AUTHORIZED_RUN_KIND: LiteralString = (
    "SELECT rn.project, rn.target_kind AS kind FROM runs rn WHERE rn.id = %s"
)
_AUTHORIZED_BOUND_RUN_KIND: LiteralString = (
    "SELECT rn.project, rn.target_kind AS kind, rn.system_id FROM runs rn WHERE rn.id = %s"
)


class _InvalidRuntimeObjectId(ValueError):
    def __init__(self, object_id: str) -> None:
        super().__init__(f"invalid provider runtime object id: {object_id}")
        self.object_id = object_id


async def _runtime_for_object(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    object_id: str,
    lookup: _RuntimeLookup,
) -> ProviderRuntime:
    uid = as_uuid(object_id)
    if uid is None:
        raise _InvalidRuntimeObjectId(object_id)
    async with pool.connection() as conn:
        kind = await _authorized_kind(conn, ctx, uid, lookup)
        if lookup.runtime_resolver is not None:
            return await lookup.runtime_resolver(resolver, conn, uid)
    return resolver.resolve(kind)


async def _authorized_kind(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    lookup: _RuntimeLookup,
) -> ResourceKind:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(lookup.sql, (uid,))
        row = await cur.fetchone()
    if row is None or row["project"] not in ctx.projects:
        raise CategorizedError(
            f"{lookup.object_kind} {uid} was not found",
            category=ErrorCategory.NOT_FOUND,
            details={"object_kind": lookup.object_kind, "object_id": str(uid)},
        )
    require_role(ctx, row["project"], lookup.required_role)
    if lookup.requires_bound_run and row["system_id"] is None:
        raise CategorizedError(
            f"{lookup.object_kind} {uid} is not bound to a system",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "run_unbound"},
        )
    if row["kind"] is None:
        raise CategorizedError(
            f"{lookup.object_kind} {uid} has no resolved provider resource",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"object_kind": lookup.object_kind, "object_id": str(uid)},
        )
    return ResourceKind(row["kind"])


async def _with_runtime_for_object(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    object_id: str,
    lookup: _RuntimeLookup,
    runtime_callback: RuntimeCallback,
) -> ToolResponse:
    try:
        runtime = await _runtime_for_object(pool, resolver, ctx, object_id, lookup)
    except _InvalidRuntimeObjectId:
        return config_error(object_id)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(object_id, exc)
    return await runtime_callback(runtime)


async def _runtime_for_allocation(
    resolver: ProviderResolver, conn: AsyncConnection, allocation_id: UUID
) -> ProviderRuntime:
    return await resolver.runtime_for_allocation(conn, allocation_id)


async def _runtime_for_system(
    resolver: ProviderResolver, conn: AsyncConnection, system_id: UUID
) -> ProviderRuntime:
    return await resolver.runtime_for_system(conn, system_id)


async def _runtime_for_run(
    resolver: ProviderResolver, conn: AsyncConnection, run_id: UUID
) -> ProviderRuntime:
    return await resolver.runtime_for_run(conn, run_id)


async def with_runtime_for_allocation(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    allocation_id: str,
    runtime_callback: RuntimeCallback,
    *,
    required_role: Role,
) -> ToolResponse:
    return await _with_runtime_for_object(
        pool,
        resolver,
        ctx,
        allocation_id,
        _RuntimeLookup(
            "allocation",
            _AUTHORIZED_ALLOCATION_KIND,
            required_role,
            runtime_resolver=_runtime_for_allocation,
        ),
        runtime_callback,
    )


async def with_runtime_for_system(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    system_id: str,
    runtime_callback: RuntimeCallback,
    *,
    required_role: Role,
) -> ToolResponse:
    return await _with_runtime_for_object(
        pool,
        resolver,
        ctx,
        system_id,
        _RuntimeLookup(
            "system",
            _AUTHORIZED_SYSTEM_KIND,
            required_role,
            runtime_resolver=_runtime_for_system,
        ),
        runtime_callback,
    )


async def with_runtime_for_run(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    run_id: str,
    runtime_callback: RuntimeCallback,
    *,
    required_role: Role,
) -> ToolResponse:
    return await _with_runtime_for_object(
        pool,
        resolver,
        ctx,
        run_id,
        _RuntimeLookup(
            "run",
            _AUTHORIZED_BOUND_RUN_KIND,
            required_role,
            runtime_resolver=_runtime_for_run,
            requires_bound_run=True,
        ),
        runtime_callback,
    )


async def with_runtime_for_run_target_kind(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    run_id: str,
    runtime_callback: RuntimeCallback,
    *,
    required_role: Role,
) -> ToolResponse:
    return await _with_runtime_for_object(
        pool,
        resolver,
        ctx,
        run_id,
        _RuntimeLookup("run", _AUTHORIZED_RUN_KIND, required_role),
        runtime_callback,
    )
