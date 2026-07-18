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
    _register_resources_register_remote_libvirt(app, pool)
    _register_resources_register_local_libvirt(app, pool)
    _register_resources_register_fault_inject(app, pool)
    _register_resources_deregister(app, pool)
    _register_resources_renew(app, pool)


def _register_resources_register_remote_libvirt(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=REGISTER_REMOTE_LIBVIRT_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def resources_register_remote_libvirt(
        name: Annotated[str, Field(description="The (kind, name) identity for the new resource.")],
        cost_class: Annotated[str, Field(description="The cost class for pricing.")],
        vcpus: Annotated[
            int,
            Field(
                gt=0,
                description=(
                    "The host's vCPU size ceiling. Admission rejects a selector larger than this, "
                    "so a host registered without it is un-grantable."
                ),
            ),
        ],
        memory_mb: Annotated[
            int,
            Field(
                gt=0,
                description=(
                    "The host's memory size ceiling in MiB (admission ≤-resource-caps check)."
                ),
            ),
        ],
        host_uri: Annotated[str, Field(description="Remote-libvirt provider host URI.")],
        base_image: Annotated[str, Field(description="Registered remote-libvirt base image name.")],
        concurrent_allocation_cap: Annotated[
            int, Field(default=1, description="Per-host concurrent-allocation cap (> 0).")
        ] = 1,
        secret_refs: Annotated[
            tuple[str, ...],
            Field(
                default=(),
                description=(
                    "Credential reference strings to preflight-resolve, e.g. cert/key/CA refs. "
                    "Only the references are stored; secret bytes are never fetched or logged."
                ),
            ),
        ] = (),
        owner_project: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Owning project; defaults to the single registering project. Pass '*' for a "
                    "global (any-project) resource."
                ),
            ),
        ] = None,
    ) -> ToolResponse:
        """Register a remote-libvirt runtime resource."""
        return await register_remote_libvirt_resource(
            pool,
            current_context(),
            RemoteLibvirtResourceRegistration(
                name=name,
                cost_class=cost_class,
                vcpus=vcpus,
                memory_mb=memory_mb,
                host_uri=host_uri,
                base_image=base_image,
                concurrent_allocation_cap=concurrent_allocation_cap,
                secret_refs=secret_refs,
                owner_project=owner_project,
            ),
        )


def _register_resources_register_local_libvirt(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=REGISTER_LOCAL_LIBVIRT_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def resources_register_local_libvirt(
        name: Annotated[str, Field(description="The (kind, name) identity for the new resource.")],
        cost_class: Annotated[str, Field(description="The cost class for pricing.")],
        vcpus: Annotated[
            int,
            Field(
                gt=0,
                description=(
                    "The host's vCPU size ceiling. Admission rejects a selector larger than this, "
                    "so a host registered without it is un-grantable."
                ),
            ),
        ],
        memory_mb: Annotated[
            int,
            Field(
                gt=0,
                description=(
                    "The host's memory size ceiling in MiB (admission ≤-resource-caps check)."
                ),
            ),
        ],
        host_uri: Annotated[str, Field(description="Local-libvirt provider host URI.")],
        concurrent_allocation_cap: Annotated[
            int, Field(default=1, description="Per-host concurrent-allocation cap (> 0).")
        ] = 1,
        secret_refs: Annotated[
            tuple[str, ...],
            Field(
                default=(),
                description=(
                    "Credential reference strings to preflight-resolve, e.g. cert/key/CA refs. "
                    "Only the references are stored; secret bytes are never fetched or logged."
                ),
            ),
        ] = (),
        owner_project: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Owning project; defaults to the single registering project. Pass '*' for a "
                    "global (any-project) resource."
                ),
            ),
        ] = None,
    ) -> ToolResponse:
        """Register a local-libvirt runtime resource."""
        return await register_local_libvirt_resource(
            pool,
            current_context(),
            LocalLibvirtResourceRegistration(
                name=name,
                cost_class=cost_class,
                vcpus=vcpus,
                memory_mb=memory_mb,
                host_uri=host_uri,
                concurrent_allocation_cap=concurrent_allocation_cap,
                secret_refs=secret_refs,
                owner_project=owner_project,
            ),
        )


def _register_resources_register_fault_inject(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=REGISTER_FAULT_INJECT_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def resources_register_fault_inject(
        name: Annotated[str, Field(description="The (kind, name) identity for the new resource.")],
        cost_class: Annotated[str, Field(description="The cost class for pricing.")],
        vcpus: Annotated[
            int,
            Field(
                gt=0,
                description=(
                    "The host's vCPU size ceiling. Admission rejects a selector larger than this, "
                    "so a host registered without it is un-grantable."
                ),
            ),
        ],
        memory_mb: Annotated[
            int,
            Field(
                gt=0,
                description=(
                    "The host's memory size ceiling in MiB (admission ≤-resource-caps check)."
                ),
            ),
        ],
        concurrent_allocation_cap: Annotated[
            int, Field(default=1, description="Per-host concurrent-allocation cap (> 0).")
        ] = 1,
        secret_refs: Annotated[
            tuple[str, ...],
            Field(
                default=(),
                description=(
                    "Credential reference strings to preflight-resolve, e.g. cert/key/CA refs. "
                    "Only the references are stored; secret bytes are never fetched or logged."
                ),
            ),
        ] = (),
        owner_project: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Owning project; defaults to the single registering project. Pass '*' for a "
                    "global (any-project) resource."
                ),
            ),
        ] = None,
    ) -> ToolResponse:
        """Register a fault-inject runtime resource."""
        return await register_fault_inject_resource(
            pool,
            current_context(),
            FaultInjectResourceRegistration(
                name=name,
                cost_class=cost_class,
                vcpus=vcpus,
                memory_mb=memory_mb,
                concurrent_allocation_cap=concurrent_allocation_cap,
                secret_refs=secret_refs,
                owner_project=owner_project,
            ),
        )


def _register_resources_deregister(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=DEREGISTER_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def resources_deregister(
        resource_id: Annotated[str, Field(description="The Resource UUID to deregister.")],
        force: Annotated[
            bool,
            Field(
                description=(
                    "Typed confirmation required to deregister a resource with live allocations "
                    "(destructive-tier)."
                )
            ),
        ] = False,
        reason: Annotated[
            str,
            Field(
                description=(
                    "Audit reason; required (non-empty) when deregistering a config-owned "
                    "remote-libvirt resource (durable removal via the override ledger). Ignored "
                    "for a runtime resource."
                )
            ),
        ] = "",
    ) -> ToolResponse:
        """Deregister a runtime or config-owned remote-libvirt resource."""
        return await deregister_resource(
            pool, current_context(), resource_id=resource_id, force=force, reason=reason
        )


def _register_resources_renew(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(name=RENEW_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def resources_renew(
        resource_id: Annotated[
            str, Field(description="The runtime Resource UUID whose lease to renew.")
        ],
    ) -> ToolResponse:
        """Renew a runtime resource lease."""
        return await renew_resource(pool, current_context(), resource_id=resource_id)


__all__ = ["register"]
