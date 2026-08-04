"""Worker job handler registry assembly."""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry import metrics
from pydantic import SecretStr

from kdive.jobs.handlers import image_build, systems
from kdive.jobs.handlers.artifacts import vmcore
from kdive.jobs.handlers.console import console_rotate
from kdive.jobs.handlers.console.capture_telemetry import CaptureTelemetry
from kdive.jobs.handlers.control import control
from kdive.jobs.handlers.runs import registrar as runs
from kdive.jobs.models import HandlerRegistry
from kdive.providers.assembly.composition import ProviderComposition
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly, build_object_store_assembly


@dataclass(frozen=True, slots=True)
class WorkerHandlerAssembly:
    """Provider/env ports assembled once for worker handler registration."""

    resolver: ProviderResolver
    incarnation_credential: SecretStr
    secret_registry: SecretRegistry
    object_stores: ObjectStoreAssembly


def build_handler_registry(
    *,
    secret_registry: SecretRegistry,
    incarnation_credential: SecretStr,
    provider_composition: ProviderComposition | None = None,
) -> HandlerRegistry:
    """Build the worker's `HandlerRegistry` from provider-aware handler registrars."""
    stores = build_object_store_assembly()
    composition = provider_composition or ProviderComposition(
        secret_registry=secret_registry, object_store=stores.store
    )
    registry = HandlerRegistry()
    assembly = WorkerHandlerAssembly(
        resolver=composition.build_provider_resolver(),
        incarnation_credential=incarnation_credential,
        secret_registry=composition.secret_registry,
        object_stores=stores,
    )
    register_all_handlers(registry, assembly)
    return registry


def register_all_handlers(registry: HandlerRegistry, assembly: WorkerHandlerAssembly) -> None:
    """Register every active worker handler using the process assembly ports."""
    from kdive.jobs.handlers import diagnostics
    from kdive.jobs.handlers.artifacts import rootfs_reclaim
    from kdive.jobs.handlers.control import capture_traffic, diagnostic_sysrq, watch_for_crash

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
