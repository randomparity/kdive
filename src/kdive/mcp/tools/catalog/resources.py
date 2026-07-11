"""The `resources.*` MCP tools (Discovery plane reads) (ADR-0023).

Thin FastMCP wrappers over plain async handlers that take the pool + request context as
arguments (tested directly, never through MCP). Resource reads are filtered by the same
project affinity predicate used by allocation placement. Response ``data`` follows the
ADR-0019 ``dict[str, JsonValue]`` contract: the nested ``capabilities`` jsonb is projected
to flat scalar fields (``kind``, ``arch``, ``vcpus``, ``memory_mb``,
``concurrent_allocation_cap``, and ``transports``), while ``resources.describe`` can add
``pool``, ``cost_class``, ``host_uri``, and provider-owned detail fields.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import RESOURCES
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.schema.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, InvalidCursor
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools._common import paginate as _paginate
from kdive.mcp.tools._resource_envelopes import resource_config_error, resource_envelope
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, projects_with_role
from kdive.services.allocation.admission.affinity import resource_visible_to_projects

_log = logging.getLogger(__name__)
type ResourceListItem = Resource | ToolResponse


class _ResourcesListPayload(ToolPayload):
    """Public payload for ``resources.list`` filters and pagination."""

    kind: ResourceKind | None = Field(
        default=None,
        description="Filter by resource kind (e.g. 'local-libvirt'); omit for all.",
    )
    limit: int = Field(
        default=DEFAULT_LIST_LIMIT,
        description=f"Maximum rows returned (capped at {MAX_LIST_LIMIT}).",
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
    )


async def _fetch_resource_rows(
    conn: AsyncConnection, kind: ResourceKind | None
) -> list[dict[str, Any]]:
    """Fetch all resource rows ascending; visibility + paging happen in the handler.

    Visibility (``resource_visible_to_projects``) is computed in Python, so paging must
    run over the *visible* list to keep ``truncated`` exact (ADR-0192). The resources table
    is the bounded operator fleet, so reading it whole per call is cheap.
    """
    if kind is None:
        query = "SELECT * FROM resources ORDER BY created_at, id"
        params: tuple[object, ...] = ()
    else:
        query = "SELECT * FROM resources WHERE kind = %s ORDER BY created_at, id"
        params = (kind.value,)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


def _resource_list_item(row: dict[str, Any]) -> ResourceListItem:
    try:
        return Resource.model_validate(row)
    except ValueError:
        object_id = row.get("id")
        _log.warning(
            "resource %s violates the response invariant; degraded",
            object_id if object_id is not None else "<missing>",
            exc_info=True,
        )
        return _resource_row_error(object_id)


def _resource_row_error(object_id: object | None) -> ToolResponse:
    return ToolResponse.failure(
        str(object_id) if object_id is not None else "resources.list",
        ErrorCategory.INFRASTRUCTURE_FAILURE,
    )


_RESOURCES_LIST_TAG = "resources.list"


def _row_visible(row: dict[str, Any], viewer_projects: tuple[str, ...]) -> bool:
    """Visibility for one raw row; a row that fails to validate is kept (degraded, not hidden)."""
    try:
        resource = Resource.model_validate(row)
    except ValueError:
        return True
    return resource_visible_to_projects(resource, viewer_projects)


async def list_resources(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    kind: str | None,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> ToolResponse:
    """Return a page of visible resources (optionally filtered by ``kind``), ascending.

    Keyset-paginated over the *visible* rows so ``data.truncated`` is exact even though
    visibility is applied in Python (ADR-0192). The fleet table is small, so it is read
    whole per call and paged in memory.
    """
    if kind is None:
        resource_kind = None
    else:
        try:
            resource_kind = ResourceKind(kind)
        except ValueError:
            return resource_config_error("resources.list")
    capped = _clamp_list_limit(limit)
    after = None
    if cursor:
        try:
            after = _decode_ts_uuid_cursor(_RESOURCES_LIST_TAG, cursor)
        except InvalidCursor:
            return _invalid_cursor_error("resources.list")
    with bind_context(principal=ctx.principal):
        viewer_projects = tuple(projects_with_role(ctx, Role.VIEWER))
        async with pool.connection() as conn:
            rows = await _fetch_resource_rows(conn, resource_kind)
        visible = [row for row in rows if _row_visible(row, viewer_projects)]
        if after is not None:
            visible = [row for row in visible if (row["created_at"], row["id"]) > after]
        kept, truncated = _paginate(visible, capped)
        next_cursor = (
            _encode_ts_uuid_cursor(_RESOURCES_LIST_TAG, kept[-1]["created_at"], kept[-1]["id"])
            if truncated and kept
            else None
        )
        responses = [_resource_envelope_or_degraded(row) for row in kept]
        return ToolResponse.collection(
            "resources",
            "ok",
            responses,
            suggested_next_actions=["resources.describe", "allocations.request"],
            data={"truncated": truncated, "next_cursor": next_cursor},
        )


def _resource_envelope_or_degraded(row: dict[str, Any]) -> ToolResponse:
    item = _resource_list_item(row)
    if isinstance(item, ToolResponse):
        return item
    return resource_envelope(item, next_actions=["resources.describe", "allocations.request"])


async def describe_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    resource_id: str,
    *,
    resolver: ProviderResolver | None = None,
) -> ToolResponse:
    """Return one resource's envelope with pool/cost_class/host_uri, or an error.

    Provider-specific adornments are owned by the bound runtime's optional
    ``resource_detail_projector`` and merged after the shared fields.
    """
    try:
        uid = UUID(resource_id)
    except ValueError:
        return resource_config_error(resource_id)
    with bind_context(principal=ctx.principal):
        viewer_projects = tuple(projects_with_role(ctx, Role.VIEWER))
        async with pool.connection() as conn:
            resource = await RESOURCES.get(conn, uid)
            if resource is None or not resource_visible_to_projects(resource, viewer_projects):
                return _not_found(resource_id)
        try:
            runtime = _runtime_for_resource(resolver, resource.kind, resource.name)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(resource_id, exc)
        provider_data = await _resource_detail_data(pool, runtime, viewer_projects)
        envelope = resource_envelope(resource, next_actions=["allocations.request"])
        envelope.data["pool"] = resource.pool
        envelope.data["cost_class"] = resource.cost_class
        envelope.data["host_uri"] = resource.host_uri
        _project_capabilities(envelope, runtime)
        envelope.data.update(provider_data)
        return envelope


def _runtime_for_resource(
    resolver: ProviderResolver | None, kind: ResourceKind, name: str | None
) -> ProviderRuntime | None:
    if resolver is None:
        return None
    runtime = resolver.resolve(kind)
    if name is not None:
        return runtime.for_resource(name)
    return runtime


async def _resource_detail_data(
    pool: AsyncConnectionPool,
    runtime: ProviderRuntime | None,
    viewer_projects: tuple[str, ...],
) -> dict[str, JsonValue]:
    if (
        runtime is None
        or runtime.resource_details is None
        or runtime.resource_details.projector is None
    ):
        return {}
    return await runtime.resource_details.projector(pool, viewer_projects)


def _project_capabilities(envelope: ToolResponse, runtime: ProviderRuntime | None) -> None:
    """Project the bound provider's capability descriptor onto a describe envelope (ADR-0208).

    Reads the resolved runtime's descriptor and emits a provider-neutral ``capabilities`` plane
    list plus the raw supported sets. No ``ResourceKind`` branching: every plane token is derived
    from the descriptor or the universal ports. The block is omitted (never an error) when no
    resolver is wired or the kind is unregistered — the same degrade-don't-fail contract as the
    staged-volume probe (ADR-0194).
    """
    if runtime is None:
        return
    capabilities: list[JsonValue] = [plane for plane in _capability_planes(runtime)]
    capture: list[JsonValue] = [
        method.value for method in sorted(runtime.support.capture_methods, key=lambda m: m.value)
    ]
    transports: list[JsonValue] = [t for t in sorted(runtime.support.debug_transports)]
    introspection: list[JsonValue] = [m for m in sorted(runtime.support.introspection)]
    envelope.data["capabilities"] = capabilities
    envelope.data["supported_capture_methods"] = capture
    envelope.data["supported_debug_transports"] = transports
    envelope.data["supported_introspection"] = introspection


def _capability_planes(runtime: ProviderRuntime) -> list[str]:
    """The sorted supported-plane tokens for ``runtime``, derived only from the descriptor.

    ``build``/``boot`` are universal (every runtime wires a builder/booter). ``kdump`` and
    ``host-dump`` track the core-producing capture methods; ``debug`` and ``introspect`` track the
    non-empty transport/introspection sets.
    """
    planes = {"build", "boot"}
    if CaptureMethod.KDUMP in runtime.support.capture_methods:
        planes.add("kdump")
    if CaptureMethod.HOST_DUMP in runtime.support.capture_methods:
        planes.add("host-dump")
    if runtime.support.debug_transports:
        planes.add("debug")
    if runtime.support.introspection:
        planes.add("introspect")
    return sorted(planes)


def register(
    app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver | None = None
) -> None:
    """Register resource catalog read tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="resources.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def resources_list(
        request: Annotated[
            _ResourcesListPayload | None,
            Field(description="Resource list filters and pagination request."),
        ] = None,
    ) -> ToolResponse:
        """List runtime resources visible to the caller.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page.
        """
        payload = request or _ResourcesListPayload()
        kind = payload.kind.value if payload.kind is not None else None
        return await list_resources(
            pool, current_context(), kind=kind, limit=payload.limit, cursor=payload.cursor
        )

    @app.tool(
        name="resources.describe",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def resources_describe(
        resource_id: Annotated[str, Field(description="The Resource UUID to describe.")],
    ) -> ToolResponse:
        """Return one runtime resource visible to the caller."""
        return await describe_resource(pool, current_context(), resource_id, resolver=resolver)
