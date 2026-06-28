"""``build_hosts.*`` and ``build_envs.*`` MCP tool registration (ADR-0099, ADR-0242).

Registers six tools on the FastMCP ``app``:

* ``build_hosts.register_ssh`` — add a new SSH build host (platform_admin, mutating).
* ``build_hosts.register_ephemeral_libvirt`` — add a new ephemeral-libvirt build host.
* ``build_hosts.list``    — enumerate all hosts (read-only).
* ``build_hosts.disable`` — set enabled=false on a host (platform_admin, mutating).
* ``build_hosts.remove``  — delete a host row (platform_admin, mutating).
* ``build_envs.list``     — contributor-readable projection of build hosts (ADR-0242).
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops.build_hosts.build_envs import list_build_envs
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

BUILD_ENVS_LIST_TOOL = "build_envs.list"


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``build_hosts.*`` and ``build_envs.*`` tools on ``app``, bound to ``pool``."""
    _register_build_hosts_register_ssh(app, pool)
    _register_build_hosts_register_ephemeral_libvirt(app, pool)
    _register_build_hosts_list(app, pool)
    _register_build_hosts_disable(app, pool)
    _register_build_hosts_remove(app, pool)
    _register_build_envs_list(app, pool)


def _register_build_hosts_register_ssh(app: FastMCP, pool: AsyncConnectionPool) -> None:
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
        """Register an SSH build host."""
        return await register_ssh_build_host(pool, current_context(), request)


def _register_build_hosts_register_ephemeral_libvirt(
    app: FastMCP, pool: AsyncConnectionPool
) -> None:
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
        """Register an ephemeral-libvirt build host."""
        return await register_ephemeral_libvirt_build_host(pool, current_context(), request)


def _register_build_hosts_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(name=LIST_TOOL, annotations=_docmeta.read_only(), meta={"maturity": "implemented"})
    async def build_hosts_list() -> ToolResponse:
        """List registered build hosts."""
        return await list_build_hosts(pool, current_context())


def _register_build_hosts_disable(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(name=DISABLE_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def build_hosts_disable(
        name: Annotated[str, Field(description="The build host name to disable.")],
    ) -> ToolResponse:
        """Disable a registered build host."""
        return await disable_build_host(pool, current_context(), name=name)


def _register_build_hosts_remove(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(name=REMOVE_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def build_hosts_remove(
        name: Annotated[str, Field(description="The build host name to remove.")],
        reason: Annotated[
            str,
            Field(
                description=(
                    "Audit reason; required (non-empty) when removing a config-owned build host "
                    "(durable removal via the override ledger). Ignored for a runtime host."
                )
            ),
        ] = "",
    ) -> ToolResponse:
        """Remove a registered build host."""
        return await remove_build_host(pool, current_context(), name=name, reason=reason)


def _register_build_envs_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=BUILD_ENVS_LIST_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def build_envs_list() -> ToolResponse:
        # ADR-0242: build-env discovery projection.
        """List build environments available for kernel builds."""
        async with pool.connection() as conn:
            return await list_build_envs(conn, current_context())


__all__ = ["register"]
