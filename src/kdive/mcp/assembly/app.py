"""FastMCP application assembly facade."""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from opentelemetry import metrics, trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import Tracer
from psycopg_pool import AsyncConnectionPool

from kdive.assembly import ProcessAssembly, build_process_assembly
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
from kdive.processes.lifecycle.worker_incarnation import WorkerDeathVerifier
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)


def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    process_assembly: ProcessAssembly | None = None,
    secret_registry: SecretRegistry,
    tracer: Tracer | None = None,
    meter: Meter | None = None,
    worker_death_verifier: WorkerDeathVerifier | None = None,
) -> FastMCP:
    """Construct the FastMCP app and register every plane's tools.

    Args:
        pool: The Postgres pool the recording middlewares and tool handlers write through.
        verifier: Token verifier; defaults to the configured one.
        process_assembly: Shared provider/object-store wiring; defaults to production assembly.
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
    process = process_assembly or build_process_assembly(secret_registry)
    stores = process.object_stores
    composition = process.providers
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
        capture_reapers=composition.build_reconciler_capture_reapers(),
        object_stores=stores,
        worker_death_verifier=(
            worker_death_verifier
            if worker_death_verifier is not None
            else process.worker_death_verifier
        ),
    )
    for register in build_plane_registrars(assembly):
        register(app, pool)
    advertise_envelope_output_schema(app)
    return app
