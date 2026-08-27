"""Worker job handler registry assembly."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opentelemetry import metrics
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

import kdive.config as config
from kdive.assembly import ProcessAssembly, build_process_assembly
from kdive.config.core_settings import BUILD_WORKSPACE
from kdive.jobs.capture_operations.launcher import GatedCaptureLauncher
from kdive.jobs.capture_operations.supervisor import CaptureOperationSupervisor
from kdive.jobs.handlers import diagnostics, image_build, systems
from kdive.jobs.handlers.artifacts import rootfs_reclaim, vmcore
from kdive.jobs.handlers.console import console_rotate
from kdive.jobs.handlers.console.capture_telemetry import CaptureTelemetry
from kdive.jobs.handlers.control import capture_traffic, control, diagnostic_sysrq, watch_for_crash
from kdive.jobs.handlers.runs import registrar as runs
from kdive.jobs.models import HandlerRegistry
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly


@dataclass(frozen=True, slots=True)
class WorkerHandlerAssembly:
    """Provider/env ports assembled once for worker handler registration."""

    resolver: ProviderResolver
    incarnation_credential: SecretStr
    secret_registry: SecretRegistry
    object_stores: ObjectStoreAssembly
    capture_supervisor: CaptureOperationSupervisor


def build_worker_handler_assembly(
    *,
    process_assembly: ProcessAssembly,
    incarnation_credential: SecretStr,
    pool: AsyncConnectionPool | None = None,
) -> WorkerHandlerAssembly:
    """Derive worker handler ports from one completed process assembly."""
    stores = process_assembly.object_stores
    composition = process_assembly.providers
    resolver = composition.build_provider_resolver()
    supervisor = CaptureOperationSupervisor(
        launcher=GatedCaptureLauncher(
            runtime_root=Path(config.require(BUILD_WORKSPACE)) / "capture-operations"
        ),
        credential=incarnation_credential,
        pool=pool,
    )
    return WorkerHandlerAssembly(
        resolver=resolver,
        incarnation_credential=incarnation_credential,
        secret_registry=composition.secret_registry,
        object_stores=stores,
        capture_supervisor=supervisor,
    )


def build_production_worker_handler_assembly(
    *,
    secret_registry: SecretRegistry,
    incarnation_credential: SecretStr,
    pool: AsyncConnectionPool | None = None,
) -> WorkerHandlerAssembly:
    """Build the production process graph and derive worker handler ports from it."""
    return build_worker_handler_assembly(
        process_assembly=build_process_assembly(secret_registry),
        incarnation_credential=incarnation_credential,
        pool=pool,
    )


def build_handler_registry(assembly: WorkerHandlerAssembly) -> HandlerRegistry:
    """Build the worker's registry from one completed handler assembly."""
    registry = HandlerRegistry()
    register_all_handlers(registry, assembly)
    return registry


def build_production_handler_registry(
    *,
    secret_registry: SecretRegistry,
    incarnation_credential: SecretStr,
    pool: AsyncConnectionPool | None = None,
) -> HandlerRegistry:
    """Build the production handler assembly and register it."""
    return build_handler_registry(
        build_production_worker_handler_assembly(
            secret_registry=secret_registry,
            incarnation_credential=incarnation_credential,
            pool=pool,
        )
    )


def register_all_handlers(registry: HandlerRegistry, assembly: WorkerHandlerAssembly) -> None:
    """Register every active worker handler using the process assembly ports."""
    systems.register_handlers(
        registry,
        resolver=assembly.resolver,
        secret_registry=assembly.secret_registry,
        artifact_store=assembly.object_stores.store,
    )
    runs.register_handlers(
        registry,
        ports=runs.RunHandlerPorts(
            resolver=assembly.resolver,
            incarnation_credential=assembly.incarnation_credential,
            secret_registry=assembly.secret_registry,
            artifact_store=assembly.object_stores.store,
        ),
    )
    console_rotate.register_handlers(
        registry,
        secret_registry=assembly.secret_registry,
        artifact_store=assembly.object_stores.store,
    )
    control.register_handlers(registry, resolver=assembly.resolver)

    diagnostic_sysrq.register_handlers(
        registry,
        resolver=assembly.resolver,
        secret_registry=assembly.secret_registry,
        artifact_store=assembly.object_stores.store,
    )

    capture_traffic.register_handlers(
        registry,
        resolver=assembly.resolver,
        artifact_store=assembly.object_stores.store,
        supervisor=assembly.capture_supervisor,
    )

    watch_for_crash.register_handlers(
        registry,
        resolver=assembly.resolver,
        secret_registry=assembly.secret_registry,
    )
    vmcore.register_handlers(
        registry,
        resolver=assembly.resolver,
        artifact_store=assembly.object_stores.store,
        telemetry=CaptureTelemetry(meter=metrics.get_meter("kdive.worker")),
    )
    image_build.register_handlers(
        registry,
        resolver=assembly.resolver,
        store=assembly.object_stores.store,
    )

    rootfs_reclaim.register_handlers(registry, artifact_store=assembly.object_stores.store)
    diagnostics.register_handlers(registry)
