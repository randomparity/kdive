"""``resources.register`` / ``deregister`` / ``renew`` MCP registration (M2.6 #396, ADR-0112).

The imperative agent-native path for runtime inventory mutation. All tools are
``platform_admin`` and mutating; ``deregister`` is destructive-tier (a live-allocation
deregister requires ``force=True``). They are registered separately from the operator host-ops
(`resources.set_status` / `set_scheduling` / `drain`) so the two concerns stay readable.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.provider_schema import assert_kind_composed
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops.resources._common import (
    DEREGISTER_TOOL,
    REGISTER_TOOL,
    RENEW_TOOL,
)
from kdive.mcp.tools.ops.resources.deregister import deregister_resource
from kdive.mcp.tools.ops.resources.register import ResourceRegistration, register_resource
from kdive.mcp.tools.ops.resources.renew import renew_resource
from kdive.providers.core.resolver import ProviderResolver


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register the runtime resource-mutation tools on ``app``, bound to ``pool``.

    ``resolver`` supplies the composed provider kinds for ``resources.register``'s call-time
    guard, which must agree with the schema narrowing ``NARROWED_TOOLS`` applies (ADR-0269 §4).
    """
    _register_resources_register(app, pool, resolver)
    _register_resources_deregister(app, pool)
    _register_resources_renew(app, pool)


def _register_resources_register(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(name=REGISTER_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def resources_register(
        kind: Annotated[
            ResourceKind,
            Field(
                description=(
                    "Provider kind to register. 'remote-libvirt' requires host_uri + base_image; "
                    "'local-libvirt' requires host_uri; 'fault-inject' takes neither (supplying "
                    "one is a configuration_error)."
                )
            ),
        ],
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
        host_uri: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Provider host URI. Required for 'remote-libvirt' and 'local-libvirt' (both "
                    "TCP-probe it for reachability before the row is written); rejected for "
                    "'fault-inject', which is synthetic and has no endpoint."
                ),
            ),
        ] = None,
        base_image: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Registered base image name. Required for 'remote-libvirt' and must already "
                    "be a registered image for that provider; rejected for the other kinds."
                ),
            ),
        ] = None,
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
        """Register a runtime resource of any provider kind (platform_admin).

        ``kind`` selects the branch: 'remote-libvirt' needs ``host_uri`` + ``base_image``,
        'local-libvirt' needs ``host_uri``, 'fault-inject' needs neither. A field that does not
        apply to the chosen kind, or a required one left blank, returns ``configuration_error``,
        as does a kind this deployment has not composed.
        """
        try:
            assert_kind_composed(kind, resolver.registered_kinds())
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(name, exc)
        return await register_resource(
            pool,
            current_context(),
            ResourceRegistration(
                kind=kind,
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
        """Deregister a runtime or config-owned resource (platform_admin). Irreversible.

        Permanently removes the resource from the inventory; there is no undo (re-add it via
        ``resources.register`` with the same kind and name). Deregistering a resource with live
        allocations requires ``force=True`` (destructive-tier).
        """
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
        """Renew a runtime resource lease (platform_admin)."""
        return await renew_resource(pool, current_context(), resource_id=resource_id)


__all__ = ["register"]
