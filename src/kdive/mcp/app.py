"""FastMCP application and worker handler assembly facades."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from opentelemetry import metrics, trace
from psycopg_pool import AsyncConnectionPool

from kdive.jobs.models import HandlerRegistry
from kdive.mcp.auth import build_verifier
from kdive.mcp.middleware.binding_errors import BindingErrorMiddleware
from kdive.mcp.middleware.denial_audit import DenialAuditMiddleware
from kdive.mcp.middleware.doc_exposure import DocExposureMiddleware
from kdive.mcp.middleware.exposure import ToolExposureMiddleware
from kdive.mcp.middleware.telemetry import TelemetryMiddleware
from kdive.mcp.middleware.usage import UsageTrackingMiddleware
from kdive.mcp.schema_advertising import advertise_envelope_output_schema
from kdive.mcp.tool_index import build_instructions
from kdive.mcp.tool_registration import PLANE_REGISTRARS, AppAssembly
from kdive.mcp.worker_registration import HANDLER_REGISTRARS, WorkerHandlerAssembly
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import build_object_store_assembly


def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    provider_composition: ProviderComposition | None = None,
    secret_registry: SecretRegistry,
) -> FastMCP:
    """Construct the FastMCP app and register every plane's tools."""
    app: FastMCP = FastMCP(
        name="kdive",
        auth=verifier or build_verifier(),
        instructions=build_instructions(),
    )
    app.add_middleware(
        TelemetryMiddleware(
            tracer=trace.get_tracer("kdive.mcp"), meter=metrics.get_meter("kdive.mcp")
        )
    )
    composition = provider_composition or ProviderComposition(secret_registry=secret_registry)
    resolver = composition.build_provider_resolver()
    app.add_middleware(UsageTrackingMiddleware(pool))
    app.add_middleware(ToolExposureMiddleware(resolver))
    app.add_middleware(DocExposureMiddleware())
    app.add_middleware(DenialAuditMiddleware(pool))
    app.add_middleware(BindingErrorMiddleware())

    assembly = AppAssembly(
        resolver=resolver,
        secret_registry=composition.secret_registry,
        reaper=composition.build_reconciler_reaper(),
        dump_volume_reaper=composition.build_reconciler_dump_volume_reaper(),
        build_vm_reaper=composition.build_reconciler_build_vm_reaper(),
        object_stores=build_object_store_assembly(),
    )
    for register in PLANE_REGISTRARS:
        register(app, pool, assembly)
    advertise_envelope_output_schema(app)
    return app


def build_handler_registry(
    *,
    secret_registry: SecretRegistry,
    provider_composition: ProviderComposition | None = None,
) -> HandlerRegistry:
    """Build the worker's `HandlerRegistry` from provider-aware handler registrars."""
    composition = provider_composition or ProviderComposition(secret_registry=secret_registry)
    registry = HandlerRegistry()
    assembly = WorkerHandlerAssembly(
        resolver=composition.build_provider_resolver(),
        secret_registry=composition.secret_registry,
        transport_factories=composition.build_build_host_transport_factories(),
        object_stores=build_object_store_assembly(),
    )
    for register in HANDLER_REGISTRARS:
        register(registry, assembly)
    return registry
