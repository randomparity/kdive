"""Table-driven MCP tool and resource registration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

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


type PlaneRegistrar = Callable[[FastMCP, AsyncConnectionPool, AppAssembly], None]


def _pool_only_plane_registrar(
    register: Callable[[FastMCP, AsyncConnectionPool], None],
) -> PlaneRegistrar:
    def _register(
        app: FastMCP,
        pool: AsyncConnectionPool,
        _: AppAssembly,
    ) -> None:
        register(app, pool)

    return _register


def _register_gateway_tools(
    app: FastMCP, _pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    gateway.register(app, resolver=assembly.resolver)


def _register_reconcile_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    ports = ops_reconcile_tools.ReconcileRepairPorts(
        reaper=assembly.reaper,
        upload_store=assembly.object_stores.optional_upload_store,
        image_store=assembly.object_stores.optional_image_store,
        dump_volume_reaper=assembly.dump_volume_reaper,
    )
    ops_reconcile_tools.register(app, pool, ports=ports)


def _register_reconcile_systems_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    ops_reconcile_systems_tools.register(
        app, pool, image_store=assembly.object_stores.optional_image_store
    )


def _register_ops_resource_host_tools(
    app: FastMCP, pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    ops_resource_host_tools.register(app, pool)


def _register_allocations_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    allocations_tools.register(app, pool, resolver=assembly.resolver)


def _register_systems_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    systems_tools.register(app, pool, resolver=assembly.resolver)


def _register_catalog_resources(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    resources.register(app, pool, resolver=assembly.resolver)


def _register_runs_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    runs_tools.register(app, pool, resolver=assembly.resolver)


def _register_control_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    control_tools.register(app, pool, resolver=assembly.resolver)


def _register_artifact_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    artifacts_tools.register(app, pool, resolver=assembly.resolver)


def _register_vmcore_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    vmcore_tools.register(
        app, pool, resolver=assembly.resolver, secret_registry=assembly.secret_registry
    )


def _register_debug_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    debug_tools.register(
        app,
        pool,
        resolver=assembly.resolver,
        secret_registry=assembly.secret_registry,
        telemetry=DebugSessionTelemetry(meter=metrics.get_meter("kdive.mcp")),
    )


def _register_introspection_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    introspect.register(
        app, pool, resolver=assembly.resolver, secret_registry=assembly.secret_registry
    )


def _register_diagnostics_tools(
    app: FastMCP, pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    def _service_factory(provider: str | None, *, with_egress: bool = False) -> DiagnosticsService:
        return default_service_factory(
            provider,
            with_egress=with_egress,
            pool=pool,
            provider_contributions=diagnostic_provider_contributions(),
        )

    ops_diagnostics_tools.register(app, pool, _service_factory)


def _register_ops_images_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    store = assembly.object_stores.optional_ops_image_store
    ops_images_tools.register(app, pool, image_store=store, upload_store=store)


def _register_ops_secrets_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    ops_secrets_tools.register(app, pool, assembly.secret_registry)


def _register_report_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    reports_generate.register(app, pool, secret_registry=assembly.secret_registry)


def _register_doc_resources(
    app: FastMCP, _pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    doc_resources.register(app, resolver=assembly.resolver)


def _register_lifecycle_prompts(
    app: FastMCP, _pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
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


PLANE_REGISTRARS: tuple[PlaneRegistrar, ...] = (
    _register_gateway_tools,
    _pool_only_plane_registrar(jobs.register),
    _register_catalog_resources,
    _pool_only_plane_registrar(availability.register),
    _pool_only_plane_registrar(projects.register),
    _pool_only_plane_registrar(session.register),
    _pool_only_plane_registrar(shapes.register),
    _pool_only_plane_registrar(register_accounting_estimate),
    _pool_only_plane_registrar(register_accounting_usage),
    _pool_only_plane_registrar(register_accounting_reports),
    _pool_only_plane_registrar(register_accounting_admin),
    _register_report_tools,
    _register_reconcile_tools,
    _register_reconcile_systems_tools,
    _register_ops_resource_host_tools,
    _pool_only_plane_registrar(ops_resource_mutation_tools.register),
    _register_allocations_tools,
    _pool_only_plane_registrar(ops_breakglass_tools.register),
    _register_systems_tools,
    _pool_only_plane_registrar(investigations.register),
    _register_runs_tools,
    _register_control_tools,
    _register_artifact_tools,
    _register_vmcore_tools,
    _register_debug_tools,
    _register_introspection_tools,
    _pool_only_plane_registrar(ops_queue_tools.register),
    _pool_only_plane_registrar(ops_tuning_tools.register),
    _pool_only_plane_registrar(ops_audit_tools.register),
    _register_diagnostics_tools,
    _pool_only_plane_registrar(ops_inventory_tools.register),
    _pool_only_plane_registrar(fixtures.register),
    _pool_only_plane_registrar(catalog_images.register),
    _pool_only_plane_registrar(kernel_config.register),
    _register_ops_images_tools,
    _register_ops_secrets_tools,
    _register_doc_resources,
    _register_lifecycle_prompts,
)
