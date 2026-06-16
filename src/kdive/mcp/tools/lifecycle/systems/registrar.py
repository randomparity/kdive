"""Registrar for the `systems.*` MCP tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT as _DEFAULT_LIST_LIMIT
from kdive.mcp.tools._runtime_resolution import with_runtime_for_allocation, with_runtime_for_system
from kdive.mcp.tools.lifecycle.systems.admin import (
    SystemAdminHandlers as _SystemAdminHandlers,
)
from kdive.mcp.tools.lifecycle.systems.admin import (
    teardown_system as _teardown_system,
)
from kdive.mcp.tools.lifecycle.systems.profile_examples import (
    build_profile_examples as _build_profile_examples,
)
from kdive.mcp.tools.lifecycle.systems.profile_examples import (
    load_inventory_for_examples as _load_inventory_for_examples,
)
from kdive.mcp.tools.lifecycle.systems.provision import (
    SystemProvisionHandlers as _SystemProvisionHandlers,
)
from kdive.mcp.tools.lifecycle.systems.view import (
    SystemsListRequest as _SystemsListRequest,
)
from kdive.mcp.tools.lifecycle.systems.view import (
    get_system as _get_system,
)
from kdive.mcp.tools.lifecycle.systems.view import (
    list_systems as _list_systems,
)
from kdive.profiles.provisioning import ProvisioningProfile, dump_profile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register the `systems.*` tools on ``app``, bound to ``pool``."""
    _register_systems_define(app, pool, resolver)
    _register_systems_provision(app, pool, resolver)
    _register_systems_provision_defined(app, pool, resolver)
    _register_systems_get(app, pool)
    _register_systems_list(app, pool)
    _register_systems_profile_examples(app)
    _register_systems_teardown(app, pool)
    _register_systems_reprovision(app, pool, resolver)


def _rootfs_validator(runtime: ProviderRuntime):
    if runtime.rootfs_validator is None:
        raise RuntimeError("systems registrar requires an injected rootfs validator")
    return runtime.rootfs_validator


def _provision_handlers(runtime: ProviderRuntime) -> _SystemProvisionHandlers:
    return _SystemProvisionHandlers(
        runtime.profile_policy, runtime.component_sources, _rootfs_validator(runtime)
    )


def _admin_handlers(runtime: ProviderRuntime) -> _SystemAdminHandlers:
    return _SystemAdminHandlers(
        runtime.profile_policy, runtime.component_sources, _rootfs_validator(runtime)
    )


def _register_systems_define(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.define",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def systems_define(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to create a DEFINED System for.")
        ],
        profile: Annotated[
            ProvisioningProfile,
            Field(
                description="Provisioning profile for the System; an 'upload' rootfs opens a "
                "pre-provision rootfs-upload window."
            ),
        ],
    ) -> ToolResponse:
        """Create a System in 'defined' for a granted Allocation (upload window). Operator only."""
        return await with_runtime_for_allocation(
            pool,
            resolver,
            allocation_id,
            lambda runtime: _provision_handlers(runtime).define_system(
                pool,
                current_context(),
                allocation_id=allocation_id,
                profile=dump_profile(profile),
            ),
        )


def _register_systems_provision(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.provision",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def systems_provision(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to provision a System for.")
        ],
        profile: Annotated[
            ProvisioningProfile,
            Field(description="Provisioning profile for the System create lane."),
        ],
    ) -> ToolResponse:
        """Mint a System for a granted Allocation and enqueue provision. Operator only."""
        return await with_runtime_for_allocation(
            pool,
            resolver,
            allocation_id,
            lambda runtime: _provision_handlers(runtime).provision_system(
                pool,
                current_context(),
                allocation_id=allocation_id,
                profile=dump_profile(profile),
            ),
        )


def _register_systems_provision_defined(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.provision_defined",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def systems_provision_defined(
        system_id: Annotated[
            str,
            Field(description="Defined System whose stored profile should be provisioned."),
        ],
    ) -> ToolResponse:
        """Admit a DEFINED System after its upload window is complete. Requires operator."""
        return await with_runtime_for_system(
            pool,
            resolver,
            system_id,
            lambda runtime: _provision_handlers(runtime).provision_defined_system(
                pool,
                current_context(),
                system_id=system_id,
            ),
        )


def _register_systems_get(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="systems.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_get(
        system_id: Annotated[str, Field(description="The System to render.")],
    ) -> ToolResponse:
        """Return a System the caller can view."""
        return await _get_system(pool, current_context(), system_id)


def _register_systems_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="systems.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_list(
        allocation_id: Annotated[
            str | None, Field(description="Only Systems under this Allocation id.")
        ] = None,
        state: Annotated[
            str | None, Field(description="Only Systems in this lifecycle state.")
        ] = None,
        shape: Annotated[
            str | None,
            Field(
                description="Only Systems with this named shape, or '__custom__' for "
                "full-custom (no shape)."
            ),
        ] = None,
        pcie: Annotated[
            str | None,
            Field(
                description="Only Systems whose Allocation claims a device matching this "
                "'<vendor>:<device>' spec."
            ),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum rows returned (capped at 200).")
        ] = _DEFAULT_LIST_LIMIT,
    ) -> ToolResponse:
        """List the caller's Systems, filterable by allocation/state/shape/PCIe. Requires viewer."""
        request = _SystemsListRequest(
            allocation_id=allocation_id,
            state=state,
            shape=shape,
            pcie=pcie,
            limit=limit,
        )
        return await _list_systems(
            pool,
            current_context(),
            request,
        )


def _register_systems_profile_examples(app: FastMCP) -> None:
    @app.tool(
        name="systems.profile_examples",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_profile_examples() -> ToolResponse:
        """Return a ready-to-edit example profile per configured provider. Requires a token."""
        # Auth-only (ADR-0117): the verifier already gated the transport; enforce token presence as
        # defence-in-depth. No platform/project gate, no audit — the projection is non-sensitive
        # inventory identifiers only (ADR-0124).
        current_context()
        return _build_profile_examples(_load_inventory_for_examples())


def _register_systems_teardown(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="systems.teardown",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def systems_teardown(
        system_id: Annotated[str, Field(description="The System to tear down.")],
    ) -> ToolResponse:
        """Enqueue teardown for a System. Requires admin on the System's project."""
        return await _teardown_system(pool, current_context(), system_id)


def _register_systems_reprovision(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.reprovision",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def systems_reprovision(
        system_id: Annotated[str, Field(description="The ready System to reprovision in place.")],
        profile: Annotated[
            ProvisioningProfile,
            Field(description="New provisioning profile; must opt in to reprovision."),
        ],
    ) -> ToolResponse:
        """Enqueue in-place reprovision for a ready System. Requires operator and opt-in."""
        return await with_runtime_for_system(
            pool,
            resolver,
            system_id,
            lambda runtime: _admin_handlers(runtime).reprovision_system(
                pool,
                current_context(),
                system_id=system_id,
                profile=dump_profile(profile),
            ),
        )
