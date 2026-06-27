"""Registrar for the `systems.*` MCP tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.capacity.state import SystemState
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import ToolPayload
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
from kdive.security.authz.rbac import Role

_LABEL_DESCRIPTION = (
    "Optional human handle for this System, echoed back as data.label in systems.get / "
    "systems.list so you thread fewer bare UUIDs. Freeform and non-unique: 1..200 printable "
    "characters (surrounding whitespace trimmed); not a lookup key. Omit for no handle."
)


class _SystemsListPayload(ToolPayload):
    """Public payload for ``systems.list`` filters and pagination."""

    allocation_id: str | None = Field(
        default=None, description="Only Systems under this Allocation id."
    )
    state: SystemState | None = Field(
        default=None, description="Only Systems in this lifecycle state."
    )
    shape: str | None = Field(
        default=None,
        description="Only Systems with this named shape, or '__custom__' for full-custom.",
    )
    pcie: str | None = Field(
        default=None,
        description="Only Systems whose Allocation claims a matching '<vendor>:<device>' spec.",
    )
    limit: int = Field(
        default=_DEFAULT_LIST_LIMIT, description="Maximum rows returned (capped at 200)."
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
    )

    def to_list_request(self) -> _SystemsListRequest:
        """Convert the public MCP payload into the handler request record."""
        return _SystemsListRequest(
            allocation_id=self.allocation_id,
            state=self.state.value if self.state is not None else None,
            shape=self.shape,
            pcie=self.pcie,
            limit=self.limit,
            cursor=self.cursor,
        )


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
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
        label: Annotated[
            str | None,
            Field(description=_LABEL_DESCRIPTION),
        ] = None,
    ) -> ToolResponse:
        """Create a System in 'defined' for a granted Allocation, opening a pre-provision
        rootfs-upload window; follow with `systems.provision_defined` once the upload is done.
        Use `systems.provision` instead when the profile needs no upload window. Operator only.
        """
        ctx = current_context()
        return await with_runtime_for_allocation(
            pool,
            resolver,
            ctx,
            allocation_id,
            lambda runtime: _provision_handlers(runtime).define_system(
                pool,
                ctx,
                allocation_id=allocation_id,
                profile=dump_profile(profile),
                idempotency_key=idempotency_key,
                label=label,
            ),
            required_role=Role.OPERATOR,
        )


def _register_systems_provision(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.provision",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_provision(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to provision a System for.")
        ],
        profile: Annotated[
            ProvisioningProfile,
            Field(description="Provisioning profile for the System create lane."),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
        label: Annotated[
            str | None,
            Field(description=_LABEL_DESCRIPTION),
        ] = None,
    ) -> ToolResponse:
        """Mint a System for a granted Allocation and enqueue provision directly (no upload
        window). Use `systems.define` then `systems.provision_defined` instead when the rootfs
        must be uploaded before provisioning. One System per Allocation: if this Allocation's
        System already failed, retrying does not mint a new one — release this Allocation and
        request a fresh one (`allocations.release`, then `allocations.request`) for a fresh
        System. Operator only.
        """
        ctx = current_context()
        return await with_runtime_for_allocation(
            pool,
            resolver,
            ctx,
            allocation_id,
            lambda runtime: _provision_handlers(runtime).provision_system(
                pool,
                ctx,
                allocation_id=allocation_id,
                profile=dump_profile(profile),
                idempotency_key=idempotency_key,
                label=label,
            ),
            required_role=Role.OPERATOR,
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
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Admit a DEFINED System after its upload window is complete; not for a fresh System —
        create it with `systems.define` first (this is the second step of that lane).
        Requires operator.
        """
        ctx = current_context()
        return await with_runtime_for_system(
            pool,
            resolver,
            ctx,
            system_id,
            lambda runtime: _provision_handlers(runtime).provision_defined_system(
                pool,
                ctx,
                system_id=system_id,
                idempotency_key=idempotency_key,
            ),
            required_role=Role.OPERATOR,
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
        request: Annotated[
            _SystemsListPayload | None,
            Field(description="Systems list filters and pagination request."),
        ] = None,
    ) -> ToolResponse:
        """List the caller's Systems, filterable by allocation/state/shape/PCIe. Requires viewer.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page.
        """
        return await _list_systems(
            pool,
            current_context(),
            (request or _SystemsListPayload()).to_list_request(),
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
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_teardown(
        system_id: Annotated[str, Field(description="The System to tear down.")],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Enqueue teardown for a System. Requires admin on the System's project."""
        return await _teardown_system(
            pool, current_context(), system_id, idempotency_key=idempotency_key
        )


def _register_systems_reprovision(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.reprovision",
        annotations=_docmeta.destructive(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_reprovision(
        system_id: Annotated[str, Field(description="The ready System to reprovision in place.")],
        profile: Annotated[
            ProvisioningProfile,
            Field(description="New provisioning profile; must opt in to reprovision."),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Enqueue in-place reprovision for a ready System; not for creating a new System —
        use `systems.provision` instead. Requires operator and opt-in.
        """
        ctx = current_context()
        return await with_runtime_for_system(
            pool,
            resolver,
            ctx,
            system_id,
            lambda runtime: _admin_handlers(runtime).reprovision_system(
                pool,
                ctx,
                system_id=system_id,
                profile=dump_profile(profile),
                idempotency_key=idempotency_key,
            ),
            required_role=Role.OPERATOR,
        )
