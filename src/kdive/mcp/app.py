"""FastMCP application assembly and the two plane registrar seams.

Tool registration and worker-handler registration are both table-driven. A plane adds
tool registrars to ``_PLANE_REGISTRARS`` and long-running job handlers to
``_HANDLER_REGISTRARS``; the entrypoint stays stable. Provider-aware registrars receive
the assembled provider/env ports (ADR-0071), while read-only/cancel-only tool groups register
no job handler because they do not own a ``JobKind``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools import Tool
from opentelemetry import metrics, trace
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.service import DiagnosticsService, default_service_factory
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers import control, image_build, runs, systems, vmcore
from kdive.jobs.handlers.capture_telemetry import CaptureTelemetry
from kdive.jobs.models import HandlerRegistry, JobHandler
from kdive.mcp.auth import build_verifier
from kdive.mcp.middleware import (
    BindingErrorMiddleware,
    DenialAuditMiddleware,
    TelemetryMiddleware,
    ToolExposureMiddleware,
    UsageTrackingMiddleware,
)
from kdive.mcp.prompts import registrar as lifecycle_prompts
from kdive.mcp.resources import registrar as doc_resources
from kdive.mcp.tools.accounting.admin import register as register_accounting_admin
from kdive.mcp.tools.accounting.estimate import register as register_accounting_estimate
from kdive.mcp.tools.accounting.reports import register as register_accounting_reports
from kdive.mcp.tools.accounting.usage import register as register_accounting_usage
from kdive.mcp.tools.catalog import (
    availability,
    build_configs,
    fixtures,
    investigations,
    jobs,
    projects,
    resources,
    session,
    shapes,
)
from kdive.mcp.tools.catalog import images as catalog_images
from kdive.mcp.tools.catalog.artifacts import registrar as artifacts_tools
from kdive.mcp.tools.debug import introspect
from kdive.mcp.tools.debug import sessions as debug_tools
from kdive.mcp.tools.lifecycle import control as control_tools
from kdive.mcp.tools.lifecycle import vmcore as vmcore_tools
from kdive.mcp.tools.lifecycle.allocations import registrar as allocations_tools
from kdive.mcp.tools.lifecycle.runs import registrar as runs_tools
from kdive.mcp.tools.lifecycle.systems import registrar as systems_tools
from kdive.mcp.tools.ops import audit as audit_tools
from kdive.mcp.tools.ops import breakglass as ops_breakglass_tools
from kdive.mcp.tools.ops import diagnostics as ops_diagnostics_tools
from kdive.mcp.tools.ops import inventory as inventory_tools
from kdive.mcp.tools.ops import queue as ops_queue_tools
from kdive.mcp.tools.ops import reconcile as ops_reconcile_tools
from kdive.mcp.tools.ops import reconcile_systems as ops_reconcile_systems_tools
from kdive.mcp.tools.ops import secrets as ops_secrets_tools
from kdive.mcp.tools.ops import tuning as ops_tuning_tools
from kdive.mcp.tools.ops.build_hosts import registrar as ops_build_hosts_tools
from kdive.mcp.tools.ops.images import registrar as ops_images_tools
from kdive.mcp.tools.ops.resources import host_ops as ops_resource_host_tools
from kdive.mcp.tools.ops.resources import registrar as ops_resource_mutation_tools
from kdive.mcp.tools.reports import register as register_report_tools
from kdive.observability.debug_session_telemetry import DebugSessionTelemetry
from kdive.providers.assembly.composition import ProviderComposition
from kdive.providers.assembly.diagnostics import diagnostic_provider_contributions
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.infra.reaping import BuildVmReaper, DumpVolumeReaper, InfraReaper
from kdive.providers.shared.build_host.dispatch import BuildHostTransportFactories
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly, build_object_store_assembly


@dataclass(frozen=True, slots=True)
class AppAssembly:
    """Provider/env ports assembled once for MCP tool registration."""

    resolver: ProviderResolver
    secret_registry: SecretRegistry
    reaper: InfraReaper
    dump_volume_reaper: DumpVolumeReaper
    build_vm_reaper: BuildVmReaper
    object_stores: ObjectStoreAssembly


@dataclass(frozen=True, slots=True)
class WorkerHandlerAssembly:
    """Provider/env ports assembled once for worker handler registration."""

    resolver: ProviderResolver
    secret_registry: SecretRegistry
    transport_factories: BuildHostTransportFactories | None
    object_stores: ObjectStoreAssembly


type PlaneRegistrar = Callable[[FastMCP, AsyncConnectionPool, AppAssembly], None]
type HandlerRegistrar = Callable[[HandlerRegistry, WorkerHandlerAssembly], None]


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


def _register_reconcile_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    ports = ops_reconcile_tools.ReconcileRepairPorts(
        reaper=assembly.reaper,
        upload_store=assembly.object_stores.optional_upload_store,
        image_store=assembly.object_stores.optional_image_store,
        dump_volume_reaper=assembly.dump_volume_reaper,
        build_vm_reaper=assembly.build_vm_reaper,
    )
    ops_reconcile_tools.register(
        app,
        pool,
        ports=ports,
    )


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


def _register_build_config_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    build_configs.register(
        app,
        pool,
        store_factory=assembly.object_stores.request_time_store_factory,
    )


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
    introspect.register(app, pool, resolver=assembly.resolver)


def _register_diagnostics_tools(
    app: FastMCP, pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    # Bind the pool into the factory with an explicit closure (not functools.partial, which strict
    # ty rejects against the parameterized ServiceFactory Protocol) so worker-vantage checks can
    # dispatch to the worker (ADR-0164).
    def _service_factory(
        provider: str | None, *, with_egress: bool = False, with_buildhost_agent: bool = False
    ) -> DiagnosticsService:
        return default_service_factory(
            provider,
            with_egress=with_egress,
            with_buildhost_agent=with_buildhost_agent,
            pool=pool,
            provider_contributions=diagnostic_provider_contributions(),
        )

    ops_diagnostics_tools.register(app, pool, _service_factory)


def _register_ops_build_hosts_tools(
    app: FastMCP, pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    ops_build_hosts_tools.register(app, pool)


def _register_ops_images_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    store = assembly.object_stores.optional_ops_image_store
    ops_images_tools.register(app, pool, image_store=store, upload_store=store)


def _register_ops_secrets_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    ops_secrets_tools.register(app, pool, assembly.secret_registry)


def _register_doc_resources(
    app: FastMCP, _pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    """Register the allowlisted operator docs as MCP resources (ADR-0151).

    Resources need neither the pool nor the provider assembly; this adapter keeps the
    ``PlaneRegistrar`` seam uniform.
    """
    doc_resources.register(app)


def _register_lifecycle_prompts(
    app: FastMCP, _pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    """Register the canonical lifecycle prompts (ADR-0202).

    Reads each registered tool's maturity from the live registry and passes it to the pure
    prompts registrar, which tags every ``partial`` step and fails fast on an unknown or
    ``planned`` referenced tool. Must run after every tool registrar (it reads tool metas);
    it is appended last in ``_PLANE_REGISTRARS``.
    """
    tool_maturity = {
        tool.name: lifecycle_prompts.ToolMaturity(
            maturity=(tool.meta or {}).get("maturity", "implemented"),
            reason=(tool.meta or {}).get("maturity_detail", {}).get("reason"),
        )
        for tool in _registered_tools(app)
    }
    lifecycle_prompts.register(app, tool_maturity=tool_maturity)


def _register_system_handlers(
    registry: HandlerRegistry,
    assembly: WorkerHandlerAssembly,
) -> None:
    systems.register_handlers(
        registry,
        resolver=assembly.resolver,
        artifact_store=assembly.object_stores.optional_upload_store,
    )


def _register_run_handlers(
    registry: HandlerRegistry,
    assembly: WorkerHandlerAssembly,
) -> None:
    from kdive.jobs.build_telemetry import BuildPhaseRecorder

    runs.register_handlers(
        registry,
        ports=runs.RunHandlerPorts(
            resolver=assembly.resolver,
            secret_registry=assembly.secret_registry,
            transport_factories=assembly.transport_factories,
            artifact_store=assembly.object_stores.optional_upload_store,
            build_phase_recorder=BuildPhaseRecorder(meter=metrics.get_meter("kdive.worker")),
        ),
    )


def _register_control_handlers(
    registry: HandlerRegistry,
    assembly: WorkerHandlerAssembly,
) -> None:
    control.register_handlers(registry, resolver=assembly.resolver)


def _register_vmcore_handlers(
    registry: HandlerRegistry,
    assembly: WorkerHandlerAssembly,
) -> None:
    vmcore.register_handlers(
        registry,
        resolver=assembly.resolver,
        telemetry=CaptureTelemetry(meter=metrics.get_meter("kdive.worker")),
    )


def _register_diagnostics_handlers(
    registry: HandlerRegistry,
    _assembly: WorkerHandlerAssembly,
) -> None:
    from kdive.jobs.handlers import diagnostics as diagnostics_handler

    diagnostics_handler.register_handlers(registry)


# Tool seam: each plane exposes register(app, pool); provider-aware planes receive AppAssembly.
_PLANE_REGISTRARS: tuple[PlaneRegistrar, ...] = (
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
    _pool_only_plane_registrar(register_report_tools),
    _register_reconcile_tools,
    _register_reconcile_systems_tools,
    _register_ops_resource_host_tools,
    _pool_only_plane_registrar(ops_resource_mutation_tools.register),
    _pool_only_plane_registrar(allocations_tools.register),
    _pool_only_plane_registrar(ops_breakglass_tools.register),
    _register_systems_tools,
    _pool_only_plane_registrar(investigations.register),
    _register_runs_tools,
    _register_control_tools,
    _register_artifact_tools,
    _register_build_config_tools,
    _register_vmcore_tools,
    _register_debug_tools,
    _register_introspection_tools,
    _pool_only_plane_registrar(ops_queue_tools.register),
    _pool_only_plane_registrar(ops_tuning_tools.register),
    _pool_only_plane_registrar(audit_tools.register),
    _register_diagnostics_tools,
    _pool_only_plane_registrar(inventory_tools.register),
    _pool_only_plane_registrar(fixtures.register),
    _pool_only_plane_registrar(catalog_images.register),
    _register_ops_build_hosts_tools,
    _register_ops_images_tools,
    _register_ops_secrets_tools,
    _register_doc_resources,
    # Must stay last: reads every registered tool's maturity to render the prompts (ADR-0202).
    _register_lifecycle_prompts,
)


def _register_image_build_handler(
    registry: HandlerRegistry,
    assembly: WorkerHandlerAssembly,
) -> None:
    """Bind the IMAGE_BUILD handler, preserving setup errors as job failures.

    The handler resolves the provider's rootfs build plane through ``ProviderResolver``; the S3
    image store is still assembled once at worker registration. A worker with no ``KDIVE_S3_*``
    env still binds IMAGE_BUILD so queued jobs fail with the original configuration category
    instead of falling through to ``not_implemented``.
    """
    store = assembly.object_stores.required_image_build_store
    if isinstance(store, CategorizedError):
        registry.register(JobKind.IMAGE_BUILD, _unconfigured_image_build_handler(store))
        return
    image_build.register_handlers(
        registry,
        resolver=assembly.resolver,
        store=store,
    )


def _unconfigured_image_build_handler(
    error: CategorizedError,
) -> JobHandler:
    async def _handler(_conn: AsyncConnection, _job: Job) -> str | None:
        raise CategorizedError(
            str(error), category=error.category, details=error.details
        ) from error

    return _handler


# Handler seam: worker modules expose register_handlers(registry). Long-running lifecycle,
# build, control, and retrieval operations register JobKind handlers here; synchronous tools
# register only in _PLANE_REGISTRARS. Handler construction receives the provider resolver and
# redaction registry without opening provider or toolchain connections at registration time.
_HANDLER_REGISTRARS: tuple[HandlerRegistrar, ...] = (
    _register_system_handlers,
    _register_run_handlers,
    _register_control_handlers,
    _register_vmcore_handlers,
    _register_image_build_handler,
    _register_diagnostics_handlers,
)


# A fielded, non-recursive output schema advertised for every tool (ADR-0170, revisiting
# ADR-0113). Every tool returns the self-referential `ToolResponse` (`items: list[ToolResponse]` +
# recursive `JsonValue` data), so FastMCP would auto-derive a recursive `$ref` schema that the
# FastMCP 3.4.0 client cannot build a validator for — it logs a per-call parse error and nulls
# `CallToolResult.data`. This schema documents every top-level envelope field while collapsing the
# two recursive fields — `data` to a bare object and `items` to an array of bare objects — so it
# carries no self-`$ref` and the client builds a validator. No field is `required` and
# `additionalProperties` is left permissive, so the client never rejects a real payload; a new
# envelope field is caught by the drift-guard test, not silently. The `structured_content` wire
# payload is unchanged (no `x-fastmcp-wrap-result` key). Typed `dict[str, Any]` to match FastMCP's
# `Tool.output_schema` and because a JSON schema nests non-str values.
ENVELOPE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The uniform kdive ToolResponse envelope (ADR-0019). `data` and `items` are "
        "intentionally open; see resource://kdive/docs/guide/response-envelope.md."
    ),
    "properties": {
        "object_id": {"type": "string"},
        "status": {"type": "string"},
        "suggested_next_actions": {"type": "array", "items": {"type": "string"}},
        "refs": {"type": "object", "additionalProperties": {"type": "string"}},
        "error_category": {"type": ["string", "null"]},
        "retryable": {"type": ["boolean", "null"]},
        "detail": {"type": ["string", "null"]},
        "data": {"type": "object"},
        "items": {"type": "array", "items": {"type": "object"}},
    },
}


def _registered_tools(app: FastMCP) -> Iterator[Tool]:
    """Yield each registered `Tool` from the local provider's component store.

    Concentrates the one private-registry accessor (`app.local_provider._components`) used by
    both the envelope-schema sweep (ADR-0170) and the lifecycle-prompts maturity read
    (ADR-0202), so the FastMCP-internals coupling lives in a single place.
    """
    for component in app.local_provider._components.values():
        if isinstance(component, Tool):
            yield component


def _advertise_envelope_output_schema(app: FastMCP) -> int:
    """Override every registered tool's advertised `outputSchema` with the envelope schema.

    Mutates the *live* registered `Tool` instances (the `Tool`-typed values in the local
    provider's component store); `app.list_tools()` returns copies whose mutation would not change
    what the server advertises. Raises if no tools are found: `build_app` always registers a
    non-empty surface, so a zero count means the FastMCP registry accessor changed under us and
    the app must not silently fall back to advertising the recursive auto-schema (ADR-0170).

    Returns the number of tools swept.
    """
    swept = 0
    for tool in _registered_tools(app):
        tool.output_schema = dict(ENVELOPE_OUTPUT_SCHEMA)
        swept += 1
    if swept == 0:
        raise RuntimeError(
            "no tools found to advertise an envelope outputSchema for; the FastMCP registry "
            "accessor (app.local_provider._components) may have changed (ADR-0170)"
        )
    return swept


def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    provider_composition: ProviderComposition | None = None,
    secret_registry: SecretRegistry,
) -> FastMCP:
    """Construct the FastMCP app and register every plane's tools.

    Args:
        pool: The shared async connection pool tools read through.
        verifier: An injected verifier (tests pass a local-keypair one); when
            ``None``, built from the OIDC env vars via :func:`build_verifier`.
        provider_composition: Provider assembly owner used when the app constructs its own
            resolver/reaper pair.
        secret_registry: App-owned registry shared by secret backends and logging.
    """
    app: FastMCP = FastMCP(name="kdive", auth=verifier or build_verifier())
    # Telemetry runs outermost (added first) so its span/RED metrics wrap the whole
    # dispatch, including a denial mapped by DenialAuditMiddleware (ADR-0090 §5). Both
    # use the process-global OTel providers, which no-op until init_telemetry runs.
    app.add_middleware(
        TelemetryMiddleware(
            tracer=trace.get_tracer("kdive.mcp"), meter=metrics.get_meter("kdive.mcp")
        )
    )
    # Added just inside telemetry: UsageTrackingMiddleware observes the final outcome after
    # DenialAuditMiddleware (added below, so inner) converts a denial to an envelope; it
    # records one best-effort tool_invocation row per call (ADR-0148). ToolExposureMiddleware
    # hooks only on_list_tools (filtering the advertised catalog), so its on_call_tool
    # position is immaterial.
    app.add_middleware(UsageTrackingMiddleware(pool))
    app.add_middleware(ToolExposureMiddleware())
    app.add_middleware(DenialAuditMiddleware(pool))
    # Innermost (added last) so it sits adjacent to argument binding: it converts a recognised
    # binding ValidationError (a typed profile, ADR-0124; the allocations.request shape-XOR rule,
    # ADR-0132) into a returned envelope, which the telemetry span above then sees as a normal
    # completion rather than an error.
    app.add_middleware(BindingErrorMiddleware())
    composition = provider_composition or ProviderComposition(secret_registry=secret_registry)
    assembly = AppAssembly(
        resolver=composition.build_provider_resolver(),
        secret_registry=composition.secret_registry,
        reaper=composition.build_reconciler_reaper(),
        dump_volume_reaper=composition.build_reconciler_dump_volume_reaper(),
        build_vm_reaper=composition.build_reconciler_build_vm_reaper(),
        object_stores=build_object_store_assembly(),
    )
    for register in _PLANE_REGISTRARS:
        register(app, pool, assembly)
    _advertise_envelope_output_schema(app)
    return app


def build_handler_registry(
    *,
    secret_registry: SecretRegistry,
    provider_composition: ProviderComposition | None = None,
) -> HandlerRegistry:
    """Build the worker's `HandlerRegistry` from provider-aware handler registrars.

    Args:
        secret_registry: Worker-owned registry shared by redaction boundaries and logging.
        provider_composition: Provider assembly owner used when the worker constructs its
            provider resolver and provider-owned support ports.
    """
    registry = HandlerRegistry()
    composition = provider_composition or ProviderComposition(secret_registry=secret_registry)
    assembly = WorkerHandlerAssembly(
        resolver=composition.build_provider_resolver(),
        secret_registry=secret_registry,
        transport_factories=composition.build_build_host_transport_factories(),
        object_stores=build_object_store_assembly(),
    )
    for register in _HANDLER_REGISTRARS:
        register(registry, assembly)
    return registry
