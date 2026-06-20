"""The ``inventory.list`` auditor-read tool (ADR-0062 §6).

A cross-project systems/allocations summary (host, status, project, lifecycle state) —
the fleet-wide view the operator uses to confirm a drain has emptied a host. Gated
``platform_auditor`` (satisfied by ``platform_admin``); read-audited to
``platform_audit_log``, never to the per-project ``audit_log``. Filterable by project and
resource. Thin FastMCP wrapper over a plain async handler taking the pool + request
context (tested directly, never through MCP).
"""

from __future__ import annotations

from datetime import datetime
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
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import paginate as _paginate
from kdive.mcp.tools._platform_auth import ALL_PROJECTS_SCOPE
from kdive.mcp.tools.ops import _reads
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    require_platform_role,
)

_TOOL = "inventory.list"
_OBJECT_ID = "inventory.list"


async def list_inventory(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str | None = None,
    resource_id: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> ToolResponse:
    """Cross-project systems/allocations summary; requires ``platform_auditor``.

    A dual-stream summary (allocations + systems), so it is **not** cursor-continuable
    (ADR-0192): each stream is capped at ``limit`` and ``data.truncated`` is true iff either
    stream was capped. An operator narrows with the ``project`` / ``resource_id`` filters.
    """
    with bind_context(principal=ctx.principal):
        try:
            resource_uuid = _parse_resource_id(resource_id)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(_OBJECT_ID, exc, suggested_next_actions=[_TOOL])
        args = _audit_args(project, resource_uuid)
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)
        except AuthorizationError:
            await _reads.audit_denial(pool, ctx, tool=_TOOL, args=args)
            return ToolResponse.failure(
                _OBJECT_ID, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL]
            )
        capped = _clamp_list_limit(limit)
        async with pool.connection() as conn:
            allocations = await _fetch_allocations(conn, project, resource_uuid, capped + 1)
            systems = await _fetch_systems(conn, project, resource_uuid, capped + 1)
            await _reads.record_read(conn, ctx, tool=_TOOL, args=args)
        return _response(allocations, systems, capped)


def _parse_resource_id(resource_id: str | None) -> UUID | None:
    if resource_id is None:
        return None
    try:
        return UUID(resource_id)
    except ValueError:
        raise CategorizedError(
            f"resource_id {resource_id!r} is not a uuid",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None


async def _fetch_allocations(
    conn: AsyncConnection, project: str | None, resource_id: UUID | None, limit: int
) -> list[dict[str, object]]:
    """Read filtered ``allocations`` rows (``limit`` rows; the caller passes ``cap + 1``).

    The WHERE clause is built from **literal** fragments (so the query stays a
    ``LiteralString`` — no runtime interpolation); filters bind as ``%s`` parameters.
    """
    params: list[object] = []
    where: LiteralString = ""
    if project is not None:
        where += " AND project = %s"
        params.append(project)
    if resource_id is not None:
        where += " AND resource_id = %s"
        params.append(resource_id)
    query: LiteralString = (
        "SELECT id, resource_id, project, principal, state, lease_expiry "
        "FROM allocations WHERE true" + where + " ORDER BY created_at DESC, id DESC LIMIT %s"
    )
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


async def _fetch_systems(
    conn: AsyncConnection, project: str | None, resource_id: UUID | None, limit: int
) -> list[dict[str, object]]:
    params: list[object] = []
    where: LiteralString = ""
    if project is not None:
        where += " AND s.project = %s"
        params.append(project)
    if resource_id is not None:
        where += " AND a.resource_id = %s"
        params.append(resource_id)
    query: LiteralString = (
        "SELECT s.id, s.allocation_id, a.resource_id, r.kind AS resource_kind, s.project, "
        "s.principal, s.state, s.domain_name FROM systems s "
        "JOIN allocations a ON a.id = s.allocation_id "
        "JOIN resources r ON r.id = a.resource_id "
        "WHERE true" + where + " ORDER BY s.created_at DESC, s.id DESC LIMIT %s"
    )
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


def _audit_args(project: str | None, resource_id: UUID | None) -> dict[str, object]:
    return {
        "scope": ALL_PROJECTS_SCOPE,
        "project": project,
        "resource_id": str(resource_id) if resource_id is not None else None,
    }


def _alloc_data(row: dict[str, object]) -> dict[str, str | None]:
    expiry = row["lease_expiry"]
    return {
        "id": str(row["id"]),
        "resource_id": str(row["resource_id"]),
        "project": _as_str(row["project"]),
        "principal": _as_str(row["principal"]),
        "state": _as_str(row["state"]),
        "lease_expiry": expiry.isoformat() if isinstance(expiry, datetime) else None,
    }


def _system_data(row: dict[str, object]) -> dict[str, str | None]:
    return {
        "id": str(row["id"]),
        "allocation_id": str(row["allocation_id"]),
        "resource_id": str(row["resource_id"]),
        "resource_kind": _as_str(row["resource_kind"]),
        "project": _as_str(row["project"]),
        "principal": _as_str(row["principal"]),
        "state": _as_str(row["state"]),
        "domain_name": _as_str(row["domain_name"]),
    }


def _as_str(value: object) -> str | None:
    return None if value is None else str(value)


def _response(
    allocations: list[dict[str, object]], systems: list[dict[str, object]], limit: int
) -> ToolResponse:
    kept_allocs, alloc_truncated = _paginate(allocations, limit)
    kept_systems, sys_truncated = _paginate(systems, limit)
    items = [
        ToolResponse.success(str(row["id"]), "ok", data={"kind": "allocation", **_alloc_data(row)})
        for row in kept_allocs
    ]
    items.extend(
        ToolResponse.success(str(row["id"]), "ok", data={"kind": "system", **_system_data(row)})
        for row in kept_systems
    )
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=["audit.query"],
        data={
            "allocation_count": len(kept_allocs),
            "system_count": len(kept_systems),
            "truncated": alloc_truncated or sys_truncated,
        },
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``inventory.list`` tool on ``app``, bound to ``pool``."""

    @app.tool(
        name="inventory.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def inventory_list(
        project: Annotated[
            str | None, Field(description="Filter the summary to one project; omit for all.")
        ] = None,
        resource_id: Annotated[
            str | None, Field(description="Filter to allocations/systems on one host UUID.")
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum rows per stream returned (capped at 200).")
        ] = DEFAULT_LIST_LIMIT,
    ) -> ToolResponse:
        """Cross-project systems/allocations summary. Requires platform auditor.

        Each stream (allocations, systems) is capped at ``limit`` (newest first);
        ``data.truncated`` is ``true`` when either cap is hit. This dual-stream summary is
        not cursor-continuable — narrow with the project/resource filters instead.
        """
        return await list_inventory(
            pool, current_context(), project=project, resource_id=resource_id, limit=limit
        )
