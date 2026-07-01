"""Table-driven worker job handler registration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry import metrics
from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers import console_rotate, control, image_build, systems, vmcore
from kdive.jobs.handlers.capture_telemetry import CaptureTelemetry
from kdive.jobs.handlers.runs import registrar as runs
from kdive.jobs.models import HandlerRegistry, JobHandler
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.shared.build_host.dispatch import BuildHostTransportFactories
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly


@dataclass(frozen=True, slots=True)
class WorkerHandlerAssembly:
    """Provider/env ports assembled once for worker handler registration."""

    resolver: ProviderResolver
    secret_registry: SecretRegistry
    transport_factories: BuildHostTransportFactories | None
    object_stores: ObjectStoreAssembly


type HandlerRegistrar = Callable[[HandlerRegistry, WorkerHandlerAssembly], None]


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
    from kdive.observability.build_telemetry import BuildPhaseRecorder

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


def _register_console_rotate_handler(
    registry: HandlerRegistry,
    assembly: WorkerHandlerAssembly,
) -> None:
    console_rotate.register_handlers(
        registry,
        secret_registry=assembly.secret_registry,
        artifact_store=assembly.object_stores.optional_upload_store,
    )


def _register_control_handlers(
    registry: HandlerRegistry,
    assembly: WorkerHandlerAssembly,
) -> None:
    control.register_handlers(registry, resolver=assembly.resolver)


def _register_diagnostic_sysrq_handler(
    registry: HandlerRegistry,
    assembly: WorkerHandlerAssembly,
) -> None:
    from kdive.jobs.handlers import diagnostic_sysrq

    diagnostic_sysrq.register_handlers(
        registry,
        resolver=assembly.resolver,
        secret_registry=assembly.secret_registry,
        artifact_store=assembly.object_stores.optional_upload_store,
    )


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


def _register_image_build_handler(
    registry: HandlerRegistry,
    assembly: WorkerHandlerAssembly,
) -> None:
    """Bind the IMAGE_BUILD handler, preserving setup errors as job failures."""
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


HANDLER_REGISTRARS: tuple[HandlerRegistrar, ...] = (
    _register_system_handlers,
    _register_run_handlers,
    _register_console_rotate_handler,
    _register_control_handlers,
    _register_diagnostic_sysrq_handler,
    _register_vmcore_handlers,
    _register_image_build_handler,
    _register_diagnostics_handlers,
)
