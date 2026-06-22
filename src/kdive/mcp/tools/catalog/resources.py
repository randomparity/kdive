"""The `resources.*` MCP tools (Discovery plane reads) (ADR-0023).

Thin FastMCP wrappers over plain async handlers that take the pool + request context as
arguments (tested directly, never through MCP). Resource reads are filtered by the same
project affinity predicate used by allocation placement. Response ``data`` follows the
ADR-0019 ``dict[str, JsonValue]`` contract: the nested ``capabilities`` jsonb is projected
to flat scalar fields (``kind``, ``arch``, ``vcpus``, ``memory_mb``,
``concurrent_allocation_cap``, and ``transports``), while ``resources.describe`` can add
``pool``, ``cost_class``, ``host_uri``, and the remote-libvirt ``staged_base_images`` list.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import RESOURCES
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, InvalidCursor
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

# A probe of `{volume: status}` for the caller-visible staged remote-libvirt base-image volumes
# (ADR-0156). Injected so handler tests need no libvirt; the production default opens one
# `qemu+tls://` connection and never raises.
StagedVolumeProbe = Callable[[list[str]], Awaitable[dict[str, str]]]

_STAGED_IMAGES_SQL = """
    SELECT name, volume
    FROM image_catalog
    WHERE provider = %(provider)s
      AND volume IS NOT NULL
      AND (visibility = %(public)s
           OR (visibility = %(private)s AND owner = ANY(%(projects)s)))
    ORDER BY name, arch
"""


async def _staged_remote_images(
    conn: AsyncConnection, ctx: RequestContext
) -> list[tuple[str, str]]:
    """Caller-visible staged remote-libvirt catalog images as ``(name, volume)``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _STAGED_IMAGES_SQL,
            {
                "provider": ResourceKind.REMOTE_LIBVIRT.value,
                "public": ImageVisibility.PUBLIC.value,
                "private": ImageVisibility.PRIVATE.value,
                "projects": projects_with_role(ctx, Role.VIEWER),
            },
        )
        return [(row["name"], row["volume"]) for row in await cur.fetchall()]


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
    staged_probe: StagedVolumeProbe | None = None,
) -> ToolResponse:
    """Return one resource's envelope with pool/cost_class/host_uri, or an error.

    For a remote-libvirt resource, also report ``staged_base_images``: each caller-visible staged
    base-image volume and whether it is staged on the host's pool (ADR-0156). The live probe is
    bound to the described host (``for_resource``, ADR-0187/0194) so a reachable host reports a real
    ``staged``/``absent``/``pool_absent`` status; ``"unknown"`` means the probe could not run. The
    probe runs only after the DB connection is released, and degrades to a per-volume status — it
    never fails the describe.
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
            staged_images: list[tuple[str, str]] = []
            if resource.kind is ResourceKind.REMOTE_LIBVIRT:
                staged_images = await _staged_remote_images(conn, ctx)
        envelope = resource_envelope(resource, next_actions=["allocations.request"])
        envelope.data["pool"] = resource.pool
        envelope.data["cost_class"] = resource.cost_class
        envelope.data["host_uri"] = resource.host_uri
        _project_capabilities(envelope, resolver, resource.kind, resource.name)
        if resource.kind is ResourceKind.REMOTE_LIBVIRT:
            probe = staged_probe or _runtime_staged_probe(resolver, resource.kind, resource.name)
            statuses = (
                await probe([volume for _, volume in staged_images])
                if probe is not None and staged_images
                else {}
            )
            staged_base_images: list[JsonValue] = [
                {"name": name, "volume": volume, "staged": statuses.get(volume, "unknown")}
                for name, volume in staged_images
            ]
            envelope.data["staged_base_images"] = staged_base_images
        return envelope


def _project_capabilities(
    envelope: ToolResponse,
    resolver: ProviderResolver | None,
    kind: ResourceKind,
    name: str | None,
) -> None:
    """Project the bound provider's capability descriptor onto a describe envelope (ADR-0208).

    Reads the resolved runtime's descriptor and emits a provider-neutral ``capabilities`` plane
    list plus the raw supported sets. No ``ResourceKind`` branching: every plane token is derived
    from the descriptor or the universal ports. The block is omitted (never an error) when no
    resolver is wired or the kind is unregistered — the same degrade-don't-fail contract as the
    staged-volume probe (ADR-0194).
    """
    if resolver is None:
        return
    try:
        runtime = resolver.resolve(kind)
        if name is not None:
            runtime = runtime.for_resource(name)
    except CategorizedError:
        return
    capabilities: list[JsonValue] = [plane for plane in _capability_planes(runtime)]
    capture: list[JsonValue] = [
        method.value for method in sorted(runtime.supported_capture_methods, key=lambda m: m.value)
    ]
    transports: list[JsonValue] = [t for t in sorted(runtime.supported_debug_transports)]
    introspection: list[JsonValue] = [m for m in sorted(runtime.supported_introspection)]
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
    if CaptureMethod.KDUMP in runtime.supported_capture_methods:
        planes.add("kdump")
    if CaptureMethod.HOST_DUMP in runtime.supported_capture_methods:
        planes.add("host-dump")
    if runtime.supported_debug_transports:
        planes.add("debug")
    if runtime.supported_introspection:
        planes.add("introspect")
    return sorted(planes)


def _runtime_staged_probe(
    resolver: ProviderResolver | None, kind: ResourceKind, name: str | None
) -> StagedVolumeProbe | None:
    """Resolve the staged-volume probe bound to the described host (ADR-0187, ADR-0194).

    A present ``name`` binds the runtime to that host via ``for_resource`` so the probe connects to
    the described resource — without it the unbound remote-libvirt runtime's ``config_factory`` is
    ``unbound_remote_config`` and degrades every volume to ``"unknown"``. A ``None`` name (a
    non-reconciled resource row) keeps the prior unbound behavior.
    """
    if resolver is None:
        return None
    try:
        runtime = resolver.resolve(kind)
        if name is not None:
            runtime = runtime.for_resource(name)
        return runtime.staged_volume_probe
    except CategorizedError:
        return None


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
        kind: Annotated[
            str | None,
            Field(description="Filter by resource kind (e.g. 'local-libvirt'); omit for all."),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum rows returned (capped at 200).")
        ] = DEFAULT_LIST_LIMIT,
        cursor: Annotated[
            str | None,
            Field(description="Opaque continuation cursor from a prior page's next_cursor."),
        ] = None,
    ) -> ToolResponse:
        """List runtime resources visible to the caller.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page.
        """
        return await list_resources(pool, current_context(), kind=kind, limit=limit, cursor=cursor)

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
