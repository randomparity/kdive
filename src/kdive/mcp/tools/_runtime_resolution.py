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
from kdive.mcp.tools._common import as_uuid, invalid_uuid_error
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

type RuntimeCallback = Callable[[ProviderRuntime], Awaitable[ToolResponse]]
type RuntimeResolver = Callable[
    [ProviderResolver, AsyncConnection, UUID], Awaitable[ProviderRuntime]
]


@dataclass(frozen=True, slots=True)
class _AuthorizationLookup:
    object_kind: str
    sql: LiteralString
    required_role: Role
    requires_bound_run: bool = False


@dataclass(frozen=True, slots=True)
class _TargetKindLookup:
    object_kind: str
    sql: LiteralString
    required_role: Role


_AUTHORIZED_ALLOCATION: LiteralString = "SELECT a.project FROM allocations a WHERE a.id = %s"
_AUTHORIZED_SYSTEM: LiteralString = "SELECT s.project FROM systems s WHERE s.id = %s"
# A target-kind-only Run lookup is authorized by the Run row and selected by its committed
# ``target_kind``. It deliberately does not join System/Allocation/Resource, so unbound Runs
# (ADR-0169, ``system_id IS NULL``) resolve for ``runs.complete_build`` before
# any System exists. Bound-run operations use ``ProviderResolver.runtime_for_run`` instead, which
# applies ``ProviderRuntime.for_resource(name)`` before capability or port handoff.
_AUTHORIZED_RUN_TARGET_KIND: LiteralString = (
    "SELECT rn.project, rn.target_kind AS kind FROM runs rn WHERE rn.id = %s"
)
_AUTHORIZED_BOUND_RUN: LiteralString = (
    "SELECT rn.project, rn.system_id FROM runs rn WHERE rn.id = %s"
)


class _InvalidRuntimeObjectId(ValueError):
    def __init__(self, object_id: str) -> None:
        super().__init__(f"invalid provider runtime object id: {object_id}")
        self.object_id = object_id


async def _bound_runtime_for_object(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    object_id: str,
    lookup: _AuthorizationLookup,
    runtime_resolver: RuntimeResolver,
) -> ProviderRuntime:
    uid = as_uuid(object_id)
    if uid is None:
        raise _InvalidRuntimeObjectId(object_id)
    async with pool.connection() as conn:
        await _authorize_object(conn, ctx, uid, lookup)
        return await runtime_resolver(resolver, conn, uid)


async def _runtime_for_target_kind(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    object_id: str,
    lookup: _TargetKindLookup,
) -> ProviderRuntime:
    uid = as_uuid(object_id)
    if uid is None:
        raise _InvalidRuntimeObjectId(object_id)
    async with pool.connection() as conn:
        kind = await _authorized_target_kind(conn, ctx, uid, lookup)
    return resolver.resolve(kind)


async def _authorize_object(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    lookup: _AuthorizationLookup,
) -> None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(lookup.sql, (uid,))
        row = await cur.fetchone()
    row = _authorize_row(row, ctx, uid, lookup.object_kind, lookup.required_role)
    if lookup.requires_bound_run and row["system_id"] is None:
        raise CategorizedError(
            f"{lookup.object_kind} {uid} is not bound to a system",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "run_unbound"},
        )


async def _authorized_target_kind(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    lookup: _TargetKindLookup,
) -> ResourceKind:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(lookup.sql, (uid,))
        row = await cur.fetchone()
    row = _authorize_row(row, ctx, uid, lookup.object_kind, lookup.required_role)
    if row["kind"] is None:
        raise CategorizedError(
            f"{lookup.object_kind} {uid} has no resolved provider resource",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"object_kind": lookup.object_kind, "object_id": str(uid)},
        )
    return ResourceKind(row["kind"])


def _authorize_row(
    row: dict[str, object] | None,
    ctx: RequestContext,
    uid: UUID,
    object_kind: str,
    required_role: Role,
) -> dict[str, object]:
    if row is None:
        raise CategorizedError(
            f"{object_kind} {uid} was not found",
            category=ErrorCategory.NOT_FOUND,
            details={"object_kind": object_kind, "object_id": str(uid)},
        )
    project = row["project"]
    if not isinstance(project, str) or project not in ctx.projects:
        raise CategorizedError(
            f"{object_kind} {uid} was not found",
            category=ErrorCategory.NOT_FOUND,
            details={"object_kind": object_kind, "object_id": str(uid)},
        )
    require_role(ctx, project, required_role)
    return row


async def _with_bound_runtime_for_object(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    object_id: str,
    lookup: _AuthorizationLookup,
    runtime_resolver: RuntimeResolver,
    runtime_callback: RuntimeCallback,
) -> ToolResponse:
    try:
        runtime = await _bound_runtime_for_object(
            pool, resolver, ctx, object_id, lookup, runtime_resolver
        )
    except _InvalidRuntimeObjectId:
        return invalid_uuid_error(f"{lookup.object_kind}_id", object_id)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(object_id, exc)
    return await runtime_callback(runtime)


async def _with_target_kind_runtime_for_object(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    object_id: str,
    lookup: _TargetKindLookup,
    runtime_callback: RuntimeCallback,
) -> ToolResponse:
    try:
        runtime = await _runtime_for_target_kind(pool, resolver, ctx, object_id, lookup)
    except _InvalidRuntimeObjectId:
        return invalid_uuid_error(f"{lookup.object_kind}_id", object_id)
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
    return await _with_bound_runtime_for_object(
        pool,
        resolver,
        ctx,
        allocation_id,
        _AuthorizationLookup(
            "allocation",
            _AUTHORIZED_ALLOCATION,
            required_role,
        ),
        _runtime_for_allocation,
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
    return await _with_bound_runtime_for_object(
        pool,
        resolver,
        ctx,
        system_id,
        _AuthorizationLookup(
            "system",
            _AUTHORIZED_SYSTEM,
            required_role,
        ),
        _runtime_for_system,
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
    return await _with_bound_runtime_for_object(
        pool,
        resolver,
        ctx,
        run_id,
        _AuthorizationLookup(
            "run",
            _AUTHORIZED_BOUND_RUN,
            required_role,
            requires_bound_run=True,
        ),
        _runtime_for_run,
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
    return await _with_target_kind_runtime_for_object(
        pool,
        resolver,
        ctx,
        run_id,
        _TargetKindLookup("run", _AUTHORIZED_RUN_TARGET_KIND, required_role),
        runtime_callback,
    )
