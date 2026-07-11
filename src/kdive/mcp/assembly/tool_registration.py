"""Table-driven MCP tool and resource registration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from fastmcp import FastMCP
from opentelemetry import metrics
from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.service import DiagnosticsService, default_service_factory
from kdive.mcp.prompts import registrar as lifecycle_prompts
from kdive.mcp.resources import registrar as doc_resources
from kdive.mcp.schema.schema_advertising import registered_tools
from kdive.mcp.tools import _docmeta, gateway, jobs
from kdive.mcp.tools.accounting.admin import register as register_accounting_admin
from kdive.mcp.tools.accounting.estimate import register as register_accounting_estimate
from kdive.mcp.tools.accounting.reports import register as register_accounting_reports
from kdive.mcp.tools.accounting.usage import register as register_accounting_usage
from kdive.mcp.tools.catalog import (
    availability,
    fixtures,
    kernel_config,
    resources,
    shapes,
)
from kdive.mcp.tools.catalog import images as catalog_images
from kdive.mcp.tools.catalog.artifacts import registrar as artifacts_tools
from kdive.mcp.tools.debug.introspection import registrar as introspect
from kdive.mcp.tools.debug.sessions import registrar as debug_tools
from kdive.mcp.tools.identity import projects, session
from kdive.mcp.tools.lifecycle.allocations import registrar as allocations_tools
from kdive.mcp.tools.lifecycle.control import registrar as control_tools
from kdive.mcp.tools.lifecycle.investigations import registrar as investigations
from kdive.mcp.tools.lifecycle.runs import registrar as runs_tools
from kdive.mcp.tools.lifecycle.systems import registrar as systems_tools
from kdive.mcp.tools.lifecycle.vmcore import registrar as vmcore_tools
from kdive.mcp.tools.ops import diagnostics as ops_diagnostics_tools
from kdive.mcp.tools.ops import queue as ops_queue_tools
from kdive.mcp.tools.ops import tuning as ops_tuning_tools
from kdive.mcp.tools.ops.audit import registrar as ops_audit_tools
from kdive.mcp.tools.ops.images import registrar as ops_images_tools
from kdive.mcp.tools.ops.inventory import registrar as ops_inventory_tools
from kdive.mcp.tools.ops.reconcile import reconcile as ops_reconcile_tools
from kdive.mcp.tools.ops.reconcile import reconcile_systems as ops_reconcile_systems_tools
from kdive.mcp.tools.ops.resources import host_ops as ops_resource_host_tools
from kdive.mcp.tools.ops.resources import registrar as ops_resource_mutation_tools
from kdive.mcp.tools.ops.security import breakglass as ops_breakglass_tools
from kdive.mcp.tools.ops.security import secrets as ops_secrets_tools
from kdive.mcp.tools.reports import generate as reports_generate
from kdive.observability.debug_session_telemetry import DebugSessionTelemetry
from kdive.providers.assembly.diagnostics import diagnostic_provider_contributions
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.infra.reaping import DumpVolumeReaper, InfraReaper
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly


@dataclass(frozen=True, slots=True)
class AppAssembly:
    """Provider/env ports assembled once for MCP tool registration."""

    resolver: ProviderResolver
    secret_registry: SecretRegistry
    reaper: InfraReaper
    dump_volume_reaper: DumpVolumeReaper
    object_stores: ObjectStoreAssembly


type PlaneRegistrar = Callable[[FastMCP, AsyncConnectionPool], None]


class ResolverRegistrar(Protocol):
    """Registrar shape for planes that only need the provider resolver."""

    def __call__(
        self, app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver
    ) -> None: ...


def _pool_only_plane_registrar(
    register: Callable[[FastMCP, AsyncConnectionPool], None],
) -> PlaneRegistrar:
    return register


def _gateway_tools_registrar(resolver: ProviderResolver) -> PlaneRegistrar:
    def _register(app: FastMCP, _pool: AsyncConnectionPool) -> None:
        gateway.register(app, resolver=resolver)

    return _register


def _reconcile_tools_registrar(
    *,
    reaper: InfraReaper,
    dump_volume_reaper: DumpVolumeReaper,
    object_stores: ObjectStoreAssembly,
) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        ports = ops_reconcile_tools.ReconcileRepairPorts(
            reaper=reaper,
            upload_store=object_stores.optional_upload_store,
            image_store=object_stores.optional_image_store,
            dump_volume_reaper=dump_volume_reaper,
        )
        ops_reconcile_tools.register(app, pool, ports=ports)

    return _register


def _reconcile_systems_tools_registrar(object_stores: ObjectStoreAssembly) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        ops_reconcile_systems_tools.register(
            app, pool, image_store=object_stores.optional_image_store
        )

    return _register


def _resolver_tools_registrar(
    register: ResolverRegistrar,
    resolver: ProviderResolver,
) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        register(app, pool, resolver=resolver)

    return _register


def _vmcore_tools_registrar(
    resolver: ProviderResolver, secret_registry: SecretRegistry
) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        vmcore_tools.register(app, pool, resolver=resolver, secret_registry=secret_registry)

    return _register


def _debug_tools_registrar(
    resolver: ProviderResolver, secret_registry: SecretRegistry
) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        debug_tools.register(
            app,
            pool,
            resolver=resolver,
            secret_registry=secret_registry,
            telemetry=DebugSessionTelemetry(meter=metrics.get_meter("kdive.mcp")),
        )

    return _register


def _introspection_tools_registrar(
    resolver: ProviderResolver, secret_registry: SecretRegistry
) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        introspect.register(app, pool, resolver=resolver, secret_registry=secret_registry)

    return _register


def _diagnostics_tools_registrar() -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        def _service_factory(
            provider: str | None, *, with_egress: bool = False
        ) -> DiagnosticsService:
            return default_service_factory(
                provider,
                with_egress=with_egress,
                pool=pool,
                provider_contributions=diagnostic_provider_contributions(),
            )

        ops_diagnostics_tools.register(app, pool, _service_factory)

    return _register


def _ops_images_tools_registrar(object_stores: ObjectStoreAssembly) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        store = object_stores.optional_ops_image_store
        ops_images_tools.register(app, pool, image_store=store, upload_store=store)

    return _register


def _ops_secrets_tools_registrar(secret_registry: SecretRegistry) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        ops_secrets_tools.register(app, pool, secret_registry)

    return _register


def _report_tools_registrar(secret_registry: SecretRegistry) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool) -> None:
        reports_generate.register(app, pool, secret_registry=secret_registry)

    return _register


def _doc_resources_registrar(resolver: ProviderResolver) -> PlaneRegistrar:
    def _register(app: FastMCP, _pool: AsyncConnectionPool) -> None:
        doc_resources.register(app, resolver=resolver)

    return _register


def _register_lifecycle_prompts(app: FastMCP, _pool: AsyncConnectionPool) -> None:
    def maturity_record(meta: Mapping[str, object] | None) -> lifecycle_prompts.ToolMaturity:
        meta = meta or {}
        detail = meta.get("maturity_detail")
        reason = detail.get("reason") if isinstance(detail, Mapping) else None
        return lifecycle_prompts.ToolMaturity(
            maturity=_docmeta.normalize_maturity(meta.get("maturity", "implemented")),
            reason=reason if isinstance(reason, str) else None,
        )

    tool_maturity = {tool.name: maturity_record(tool.meta) for tool in registered_tools(app)}
    lifecycle_prompts.register(app, tool_maturity=tool_maturity)


def build_plane_registrars(assembly: AppAssembly) -> tuple[PlaneRegistrar, ...]:
    """Build plane registrars from the narrow dependencies each group actually uses."""
    return (
        _gateway_tools_registrar(assembly.resolver),
        _pool_only_plane_registrar(jobs.register),
        _resolver_tools_registrar(resources.register, assembly.resolver),
        _pool_only_plane_registrar(availability.register),
        _pool_only_plane_registrar(projects.register),
        _pool_only_plane_registrar(session.register),
        _pool_only_plane_registrar(shapes.register),
        _pool_only_plane_registrar(register_accounting_estimate),
        _pool_only_plane_registrar(register_accounting_usage),
        _pool_only_plane_registrar(register_accounting_reports),
        _pool_only_plane_registrar(register_accounting_admin),
        _report_tools_registrar(assembly.secret_registry),
        _reconcile_tools_registrar(
            reaper=assembly.reaper,
            dump_volume_reaper=assembly.dump_volume_reaper,
            object_stores=assembly.object_stores,
        ),
        _reconcile_systems_tools_registrar(assembly.object_stores),
        _pool_only_plane_registrar(ops_resource_host_tools.register),
        _pool_only_plane_registrar(ops_resource_mutation_tools.register),
        _resolver_tools_registrar(allocations_tools.register, assembly.resolver),
        _pool_only_plane_registrar(ops_breakglass_tools.register),
        _resolver_tools_registrar(systems_tools.register, assembly.resolver),
        _pool_only_plane_registrar(investigations.register),
        _resolver_tools_registrar(runs_tools.register, assembly.resolver),
        _resolver_tools_registrar(control_tools.register, assembly.resolver),
        _resolver_tools_registrar(artifacts_tools.register, assembly.resolver),
        _vmcore_tools_registrar(assembly.resolver, assembly.secret_registry),
        _debug_tools_registrar(assembly.resolver, assembly.secret_registry),
        _introspection_tools_registrar(assembly.resolver, assembly.secret_registry),
        _pool_only_plane_registrar(ops_queue_tools.register),
        _pool_only_plane_registrar(ops_tuning_tools.register),
        _pool_only_plane_registrar(ops_audit_tools.register),
        _diagnostics_tools_registrar(),
        _pool_only_plane_registrar(ops_inventory_tools.register),
        _pool_only_plane_registrar(fixtures.register),
        _pool_only_plane_registrar(catalog_images.register),
        _pool_only_plane_registrar(kernel_config.register),
        _ops_images_tools_registrar(assembly.object_stores),
        _ops_secrets_tools_registrar(assembly.secret_registry),
        _doc_resources_registrar(assembly.resolver),
        _register_lifecycle_prompts,
    )
