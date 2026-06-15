"""``build_hosts.*`` MCP tool registration (ADR-0099, issue #342).

Registers five tools on the FastMCP ``app``:

* ``build_hosts.register_ssh`` — add a new SSH build host (platform_admin, mutating).
* ``build_hosts.register_ephemeral_libvirt`` — add a new ephemeral-libvirt build host.
* ``build_hosts.list``    — enumerate all hosts (read-only).
* ``build_hosts.disable`` — set enabled=false on a host (platform_admin, mutating).
* ``build_hosts.remove``  — delete a host row (platform_admin, mutating).
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops.build_hosts.lifecycle import (
    DISABLE_TOOL,
    LIST_TOOL,
    REMOVE_TOOL,
    disable_build_host,
    list_build_hosts,
    remove_build_host,
)
from kdive.mcp.tools.ops.build_hosts.register import (
    REGISTER_EPHEMERAL_LIBVIRT_TOOL,
    REGISTER_SSH_TOOL,
    EphemeralLibvirtBuildHostRegistration,
    SshBuildHostRegistration,
    register_ephemeral_libvirt_build_host,
    register_ssh_build_host,
)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``build_hosts.*`` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name=REGISTER_SSH_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def build_hosts_register_ssh(
        request: Annotated[
            SshBuildHostRegistration,
            Field(description="SSH build-host registration request."),
        ],
    ) -> ToolResponse:
        return await register_ssh_build_host(pool, current_context(), request)

    @app.tool(
        name=REGISTER_EPHEMERAL_LIBVIRT_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def build_hosts_register_ephemeral_libvirt(
        request: Annotated[
            EphemeralLibvirtBuildHostRegistration,
            Field(description="Ephemeral-libvirt build-host registration request."),
        ],
    ) -> ToolResponse:
        return await register_ephemeral_libvirt_build_host(pool, current_context(), request)

    @app.tool(name=LIST_TOOL, annotations=_docmeta.read_only(), meta={"maturity": "implemented"})
    async def build_hosts_list() -> ToolResponse:
        return await list_build_hosts(pool, current_context())

    @app.tool(name=DISABLE_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def build_hosts_disable(
        name: Annotated[str, Field(description="The build host name to disable.")],
    ) -> ToolResponse:
        return await disable_build_host(pool, current_context(), name=name)

    @app.tool(name=REMOVE_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def build_hosts_remove(
        name: Annotated[str, Field(description="The build host name to remove.")],
    ) -> ToolResponse:
        return await remove_build_host(pool, current_context(), name=name)


__all__ = ["register"]
