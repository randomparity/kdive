"""Read-only `systems.*` MCP handlers (ADR-0025, ADR-0070)."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.sql import Composable
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import System
from kdive.domain.pcie import parse_match_spec
from kdive.log import bind_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, ConfigErrorReason
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools.debug.sessions_read import active_session_ids_for_system
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

CUSTOM_SHAPE_SENTINEL = "__custom__"
"""The ``shape`` filter value selecting full-custom Systems (``shape IS NULL``)."""


@dataclass(frozen=True, slots=True)
class SystemsListRequest:
    """Filter payload for ``systems.list``."""

    allocation_id: str | None = None
    state: str | None = None
    shape: str | None = None
    pcie: str | None = None
    limit: int = DEFAULT_LIST_LIMIT


def system_envelope(
    system: System,
    *,
    resource_kind: str | None = None,
    active_debug_session_ids: list[str] | None = None,
) -> ToolResponse:
    """Render a System; ``failed`` becomes a failure envelope.

    ``resource_kind`` (the backing Resource's kind) is included when supplied so a list caller
    can match a ready System against a Run's ``target_kind`` for ``runs.bind`` (ADR-0169).
    ``active_debug_session_ids`` (ADR-0176) lists the ids of `attach`/`live` debug sessions on
    any Run on this System, so a recovering agent can pivot from a known System to a live
    session handle; omitted (and absent from ``data``) on the list path to avoid an N+1.
    """
    data: dict[str, JsonValue] = {"project": system.project}
    if resource_kind is not None:
        data["resource_kind"] = resource_kind
    if active_debug_session_ids is not None:
        data["active_debug_session_ids"] = list(active_debug_session_ids)
    if system.state is SystemState.FAILED:
        return ToolResponse.failure(
            str(system.id),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            data={"current_status": system.state.value, **data},
        )
    return ToolResponse.success(
        str(system.id),
        system.state.value,
        suggested_next_actions=["systems.get", "systems.teardown"],
        data=data,
    )


def defined_system_envelope(system: System) -> ToolResponse:
    """Render a newly defined System with its upload/provision next actions."""
    return ToolResponse.success(
        str(system.id),
        SystemState.DEFINED.value,
        suggested_next_actions=["artifacts.create_system_upload", "systems.provision_defined"],
        data={"project": system.project},
    )


async def get_system(
    pool: AsyncConnectionPool, ctx: RequestContext, system_id: str
) -> ToolResponse:
    """Return a System the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            require_role(ctx, system.project, Role.VIEWER)
            active_sessions = await active_session_ids_for_system(conn, system.id)
        return system_envelope(system, active_debug_session_ids=active_sessions)


def _viewer_projects(ctx: RequestContext) -> list[str]:
    """Projects the caller may view: a member project with any granted role."""
    return [p for p in ctx.projects if ctx.roles.get(p) is not None]


@dataclass(frozen=True, slots=True)
class _SystemFilters:
    """The validated, SQL-ready clauses and params for a :func:`list_systems` query."""

    clauses: list[Composable]
    params: list[object]


def _build_filters(
    viewer_projects: list[str],
    *,
    allocation_id: str | None,
    state: str | None,
    shape: str | None,
    pcie: str | None,
) -> _SystemFilters | ToolResponse:
    """Translate filter args into SQL clauses, or a ``configuration_error`` envelope."""
    clauses: list[Composable] = [sql.SQL("s.project = ANY(%s)")]
    params: list[object] = [viewer_projects]
    if allocation_id is not None:
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _invalid_uuid_error("allocation_id", allocation_id)
        clauses.append(sql.SQL("s.allocation_id = %s"))
        params.append(uid)
    if state is not None:
        try:
            resolved = SystemState(state)
        except ValueError:
            return _config_error_reason(
                state,
                ConfigErrorReason.INVALID_STATE,
                accepted_values=[s.value for s in SystemState],
                detail=f"state {state!r} is not a valid System state",
            )
        clauses.append(sql.SQL("s.state = %s"))
        params.append(resolved.value)
    if shape is not None:
        if shape == CUSTOM_SHAPE_SENTINEL:
            clauses.append(sql.SQL("s.shape IS NULL"))
        else:
            clauses.append(sql.SQL("s.shape = %s"))
            params.append(shape)
    if pcie is not None:
        pcie_clause = _pcie_clause(pcie, params)
        if isinstance(pcie_clause, ToolResponse):
            return pcie_clause
        clauses.append(pcie_clause)
    return _SystemFilters(clauses, params)


def _pcie_clause(pcie: str, params: list[object]) -> Composable | ToolResponse:
    """Build the ``pcie`` SQL predicate, or a ``configuration_error`` envelope."""
    try:
        spec = parse_match_spec(pcie.strip())
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(pcie, exc)
    if spec.vendor_id is None or spec.device_id is None:
        return _config_error_reason(
            pcie,
            ConfigErrorReason.INVALID_PCIE_MATCH,
            detail="pcie match must specify both a vendor id and a device id",
        )
    params.extend([spec.vendor_id, spec.device_id])
    return sql.SQL(
        "EXISTS (SELECT 1 FROM jsonb_array_elements(a.pcie_claim) e "
        "WHERE e->>'vendor_id' = %s AND e->>'device_id' = %s)"
    )


async def list_systems(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: SystemsListRequest | None = None,
) -> ToolResponse:
    """List the caller's Systems, filterable by allocation, state, shape, and PCIe match."""
    request = request or SystemsListRequest()
    viewer_projects = _viewer_projects(ctx)
    filters = _build_filters(
        viewer_projects,
        allocation_id=request.allocation_id,
        state=request.state,
        shape=request.shape,
        pcie=request.pcie,
    )
    if isinstance(filters, ToolResponse):
        return filters
    capped = _clamp_list_limit(request.limit)
    with bind_context(principal=ctx.principal):
        if not viewer_projects:
            return _systems_collection([])
        query = sql.SQL(
            "SELECT s.*, r.kind AS resource_kind FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "JOIN resources r ON r.id = a.resource_id "
            "WHERE {where} ORDER BY s.created_at DESC, s.id LIMIT %s"
        ).format(where=sql.SQL(" AND ").join(filters.clauses))
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, (*filters.params, capped))
            rows = await cur.fetchall()
        return _systems_collection([_split_kind(row) for row in rows])


def _split_kind(row: dict[str, object]) -> tuple[System, str]:
    """Separate the joined ``resource_kind`` from the System columns before validation."""
    resource_kind = str(row.pop("resource_kind"))
    return System.model_validate(row), resource_kind


def _systems_collection(systems: list[tuple[System, str]]) -> ToolResponse:
    """Render Systems (each with its backing Resource kind) into one collection envelope."""
    return ToolResponse.collection(
        "systems",
        "ok",
        [system_envelope(system, resource_kind=resource_kind) for system, resource_kind in systems],
        suggested_next_actions=["systems.get", "runs.create"],
    )
