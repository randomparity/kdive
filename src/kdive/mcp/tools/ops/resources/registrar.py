"""``resources.register_*`` / ``deregister`` / ``renew`` MCP registration (M2.6 #396, ADR-0112).

The imperative agent-native path for runtime inventory mutation. All tools are
``platform_admin`` and mutating; ``deregister`` is destructive-tier (a live-allocation
deregister requires ``force=True``). They are registered separately from the operator host-ops
(`resources.set_status` / `cordon` / `uncordon` / `drain`) so the two concerns stay readable.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops.resources._common import (
    DEREGISTER_TOOL,
    REGISTER_FAULT_INJECT_TOOL,
    REGISTER_LOCAL_LIBVIRT_TOOL,
    REGISTER_REMOTE_LIBVIRT_TOOL,
    RENEW_TOOL,
)
from kdive.mcp.tools.ops.resources.deregister import deregister_resource
from kdive.mcp.tools.ops.resources.register import (
    FaultInjectResourceRegistration,
    LocalLibvirtResourceRegistration,
    RemoteLibvirtResourceRegistration,
    register_fault_inject_resource,
    register_local_libvirt_resource,
    register_remote_libvirt_resource,
)
from kdive.mcp.tools.ops.resources.renew import renew_resource


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the runtime resource-mutation tools on ``app``, bound to ``pool``."""

    @app.tool(
        name=REGISTER_REMOTE_LIBVIRT_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def resources_register_remote_libvirt(
        request: Annotated[
            RemoteLibvirtResourceRegistration,
            Field(description="Remote-libvirt runtime resource registration request."),
        ],
    ) -> ToolResponse:
        return await register_remote_libvirt_resource(pool, current_context(), request)

    @app.tool(
        name=REGISTER_LOCAL_LIBVIRT_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def resources_register_local_libvirt(
        request: Annotated[
            LocalLibvirtResourceRegistration,
            Field(description="Local-libvirt runtime resource registration request."),
        ],
    ) -> ToolResponse:
        return await register_local_libvirt_resource(pool, current_context(), request)

    @app.tool(
        name=REGISTER_FAULT_INJECT_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def resources_register_fault_inject(
        request: Annotated[
            FaultInjectResourceRegistration,
            Field(description="Fault-inject runtime resource registration request."),
        ],
    ) -> ToolResponse:
        return await register_fault_inject_resource(pool, current_context(), request)

    @app.tool(
        name=DEREGISTER_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def resources_deregister(
        resource_id: Annotated[str, Field(description="The runtime Resource UUID to deregister.")],
        force: Annotated[
            bool,
            Field(
                description=(
                    "Typed confirmation required to deregister a resource with live allocations "
                    "(destructive-tier)."
                )
            ),
        ] = False,
    ) -> ToolResponse:
        return await deregister_resource(
            pool, current_context(), resource_id=resource_id, force=force
        )

    @app.tool(name=RENEW_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def resources_renew(
        resource_id: Annotated[
            str, Field(description="The runtime Resource UUID whose lease to renew.")
        ],
    ) -> ToolResponse:
        return await renew_resource(pool, current_context(), resource_id=resource_id)


__all__ = ["register"]
