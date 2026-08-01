"""FastMCP application assembly facade."""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from opentelemetry import metrics, trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import Tracer
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.tool_registration import AppAssembly, build_plane_registrars
from kdive.mcp.auth import build_verifier
from kdive.mcp.exposure import gateway_enabled
from kdive.mcp.middleware.binding_errors import BindingErrorMiddleware
from kdive.mcp.middleware.compact import CompactResponseMiddleware
from kdive.mcp.middleware.denial_audit import DenialAuditMiddleware
from kdive.mcp.middleware.doc_exposure import DocExposureMiddleware
from kdive.mcp.middleware.exposure import ToolExposureMiddleware
from kdive.mcp.middleware.telemetry import TelemetryMiddleware
from kdive.mcp.middleware.usage import UsageTrackingMiddleware
from kdive.mcp.schema.schema_advertising import advertise_envelope_output_schema
from kdive.mcp.schema.tool_index import build_instructions
from kdive.mcp.verbosity import compact_responses_enabled
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly, build_object_store_assembly

_log = logging.getLogger(__name__)


def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    provider_composition: ProviderComposition | None = None,
    object_store_assembly: ObjectStoreAssembly | None = None,
    secret_registry: SecretRegistry,
    tracer: Tracer | None = None,
    meter: Meter | None = None,
) -> FastMCP:
    """Construct the FastMCP app and register every plane's tools.

    Args:
        pool: The Postgres pool the recording middlewares and tool handlers write through.
        verifier: Token verifier; defaults to the configured one.
        provider_composition: Provider wiring; defaults to one built over ``secret_registry``.
        object_store_assembly: Object-store wiring; defaults to the validated production store.
            Offline schema consumers may inject wiring that they never invoke.
        secret_registry: The app-owned registry redaction and providers read through.
        tracer: Span emitter for ``TelemetryMiddleware``; defaults to the process-global
            tracer. Injectable per ADR-0487 so telemetry can be observed for one app.
        meter: RED-metric emitter for ``TelemetryMiddleware``; defaults to the
            process-global meter, on the same terms as ``tracer``.

    Returns:
        The assembled FastMCP app.
    """
    app: FastMCP = FastMCP(
        name="kdive",
        auth=verifier or build_verifier(),
        instructions=build_instructions(gateway_enabled()),
    )
    app.add_middleware(CompactResponseMiddleware())  # first == outermost (ADR-0314)
    if compact_responses_enabled():
        _log.info("compact_responses enabled")
    # Both handles default to the process globals the observability facade installs, and
    # are overridable per app (ADR-0487): the OTel globals are set-once per process, so an
    # app's telemetry is otherwise only observable by mutating state every other app in the
    # process shares.
    app.add_middleware(
        TelemetryMiddleware(
            tracer=tracer or trace.get_tracer("kdive.mcp"),
            meter=meter or metrics.get_meter("kdive.mcp"),
        )
    )
    composition = provider_composition or ProviderComposition(secret_registry=secret_registry)
    resolver = composition.build_provider_resolver()
    app.add_middleware(UsageTrackingMiddleware(pool, secret_registry=composition.secret_registry))
    app.add_middleware(ToolExposureMiddleware(resolver))
    app.add_middleware(DocExposureMiddleware())
    app.add_middleware(DenialAuditMiddleware(pool))
    app.add_middleware(BindingErrorMiddleware())

    assembly = AppAssembly(
        resolver=resolver,
        secret_registry=composition.secret_registry,
        reaper=composition.build_reconciler_reaper(),
        dump_volume_reaper=composition.build_reconciler_dump_volume_reaper(),
        object_stores=(
            object_store_assembly
            if object_store_assembly is not None
            else build_object_store_assembly()
        ),
    )
    for register in build_plane_registrars(assembly):
        register(app, pool)
    advertise_envelope_output_schema(app)
    return app
