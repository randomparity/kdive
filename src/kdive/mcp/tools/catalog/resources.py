"""The `resources.*` MCP tools (Discovery plane reads) (ADR-0023).

Thin FastMCP wrappers over plain async handlers that take the pool + request context as
arguments (tested directly, never through MCP). Resource reads are filtered by the same
project affinity predicate used by allocation placement. The nested `capabilities` jsonb is
projected to a flat `dict[str, str]` for the response envelope (ADR-0019 `data` is
`dict[str, str]`).
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
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import ImageVisibility, Resource, ResourceKind
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._resource_envelopes import resource_config_error, resource_envelope
from kdive.providers.remote_libvirt.staged_volumes import probe_staged_volumes
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, projects_with_role
from kdive.services.allocation.admission.affinity import resource_visible_to_projects

_log = logging.getLogger(__name__)

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
    if kind is None:
        query = "SELECT * FROM resources ORDER BY created_at, id"
        params: tuple[object, ...] = ()
    else:
        query = "SELECT * FROM resources WHERE kind = %s ORDER BY created_at, id"
        params = (kind.value,)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


def _resource_row_error(row: dict[str, Any]) -> ToolResponse:
    object_id = row.get("id")
    return ToolResponse.failure(
        str(object_id) if object_id is not None else "resources.list",
        ErrorCategory.INFRASTRUCTURE_FAILURE,
    )


async def list_resources_tool(
    pool: AsyncConnectionPool, ctx: RequestContext, *, kind: str | None
) -> ToolResponse:
    """Return every resource (optionally filtered by ``kind``) in one collection envelope."""
    if kind is None:
        resource_kind = None
    else:
        try:
            resource_kind = ResourceKind(kind)
        except ValueError:
            return resource_config_error("resources.list")
    with bind_context(principal=ctx.principal):
        viewer_projects = tuple(projects_with_role(ctx, Role.VIEWER))
        async with pool.connection() as conn:
            rows = await _fetch_resource_rows(conn, resource_kind)
        responses: list[ToolResponse] = []
        for row in rows:
            try:
                resource = Resource.model_validate(row)
                if not resource_visible_to_projects(resource, viewer_projects):
                    continue
                responses.append(
                    resource_envelope(
                        resource, next_actions=["resources.describe", "allocations.request"]
                    )
                )
            except ValueError:
                _log.warning(
                    "resource %s violates the response invariant; degraded",
                    row.get("id", "<missing>"),
                    exc_info=True,
                )
                responses.append(_resource_row_error(row))
        return ToolResponse.collection(
            "resources",
            "ok",
            responses,
            suggested_next_actions=["resources.describe", "allocations.request"],
        )


async def describe_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    resource_id: str,
    *,
    staged_probe: StagedVolumeProbe | None = None,
) -> ToolResponse:
    """Return one resource's envelope with pool/cost_class/host_uri, or an error.

    For a remote-libvirt resource, also report ``staged_base_images``: each caller-visible staged
    base-image volume and whether it is staged on the host's pool (ADR-0156). The live probe runs
    only after the DB connection is released, and degrades to a per-volume status — it never fails
    the describe.
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
                return resource_config_error(resource_id)
            staged_images: list[tuple[str, str]] = []
            if resource.kind is ResourceKind.REMOTE_LIBVIRT:
                staged_images = await _staged_remote_images(conn, ctx)
        envelope = resource_envelope(resource, next_actions=["allocations.request"])
        envelope.data["pool"] = resource.pool
        envelope.data["cost_class"] = resource.cost_class
        envelope.data["host_uri"] = resource.host_uri
        if resource.kind is ResourceKind.REMOTE_LIBVIRT:
            probe = staged_probe or probe_staged_volumes
            statuses = await probe([volume for _, volume in staged_images]) if staged_images else {}
            staged_base_images: list[JsonValue] = [
                {"name": name, "volume": volume, "staged": statuses.get(volume, "unknown")}
                for name, volume in staged_images
            ]
            envelope.data["staged_base_images"] = staged_base_images
        return envelope


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
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
    ) -> ToolResponse:
        """List runtime resources visible to the caller."""
        return await list_resources_tool(pool, current_context(), kind=kind)

    @app.tool(
        name="resources.describe",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def resources_describe(
        resource_id: Annotated[str, Field(description="The Resource UUID to describe.")],
    ) -> ToolResponse:
        """Return one runtime resource visible to the caller."""
        return await describe_resource(pool, current_context(), resource_id)
