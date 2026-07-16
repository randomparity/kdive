"""Worker job handler registry assembly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry import metrics

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
    secret_registry: SecretRegistry
    object_stores: ObjectStoreAssembly


type HandlerRegistrar = Callable[[HandlerRegistry], None]


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
        object_stores=build_object_store_assembly(),
    )
    for register in build_handler_registrars(assembly):
        register(registry)
    return registry


def _system_handlers_registrar(
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    object_stores: ObjectStoreAssembly,
) -> HandlerRegistrar:
    def _register(registry: HandlerRegistry) -> None:
        systems.register_handlers(
            registry,
            resolver=resolver,
            secret_registry=secret_registry,
            artifact_store=object_stores.store,
        )

    return _register


def _run_handlers_registrar(
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    object_stores: ObjectStoreAssembly,
) -> HandlerRegistrar:
    def _register(registry: HandlerRegistry) -> None:
        runs.register_handlers(
            registry,
            ports=runs.RunHandlerPorts(
                resolver=resolver,
                secret_registry=secret_registry,
                artifact_store=object_stores.store,
            ),
        )

    return _register


def _console_rotate_handler_registrar(
    *, secret_registry: SecretRegistry, object_stores: ObjectStoreAssembly
) -> HandlerRegistrar:
    def _register(registry: HandlerRegistry) -> None:
        console_rotate.register_handlers(
            registry,
            secret_registry=secret_registry,
            artifact_store=object_stores.store,
        )

    return _register


def _control_handlers_registrar(resolver: ProviderResolver) -> HandlerRegistrar:
    def _register(registry: HandlerRegistry) -> None:
        control.register_handlers(registry, resolver=resolver)

    return _register


def _diagnostic_sysrq_handler_registrar(
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    object_stores: ObjectStoreAssembly,
) -> HandlerRegistrar:
    def _register(registry: HandlerRegistry) -> None:
        from kdive.jobs.handlers.control import diagnostic_sysrq

        diagnostic_sysrq.register_handlers(
            registry,
            resolver=resolver,
            secret_registry=secret_registry,
            artifact_store=object_stores.store,
        )

    return _register


def _watch_for_crash_handler_registrar(
    *, resolver: ProviderResolver, secret_registry: SecretRegistry
) -> HandlerRegistrar:
    def _register(registry: HandlerRegistry) -> None:
        from kdive.jobs.handlers.control import watch_for_crash

        watch_for_crash.register_handlers(
            registry, resolver=resolver, secret_registry=secret_registry
        )

    return _register


def _vmcore_handlers_registrar(resolver: ProviderResolver) -> HandlerRegistrar:
    def _register(registry: HandlerRegistry) -> None:
        vmcore.register_handlers(
            registry,
            resolver=resolver,
            telemetry=CaptureTelemetry(meter=metrics.get_meter("kdive.worker")),
        )

    return _register


def _register_diagnostics_handlers(registry: HandlerRegistry) -> None:
    from kdive.jobs.handlers import diagnostics as diagnostics_handler

    diagnostics_handler.register_handlers(registry)


def _image_build_handler_registrar(
    *, resolver: ProviderResolver, object_stores: ObjectStoreAssembly
) -> HandlerRegistrar:
    def _register(registry: HandlerRegistry) -> None:
        image_build.register_handlers(
            registry,
            resolver=resolver,
            store=object_stores.store,
        )

    return _register


def build_handler_registrars(assembly: WorkerHandlerAssembly) -> tuple[HandlerRegistrar, ...]:
    """Build worker registrars from the narrow dependencies each group uses."""
    return (
        _system_handlers_registrar(
            resolver=assembly.resolver,
            secret_registry=assembly.secret_registry,
            object_stores=assembly.object_stores,
        ),
        _run_handlers_registrar(
            resolver=assembly.resolver,
            secret_registry=assembly.secret_registry,
            object_stores=assembly.object_stores,
        ),
        _console_rotate_handler_registrar(
            secret_registry=assembly.secret_registry,
            object_stores=assembly.object_stores,
        ),
        _control_handlers_registrar(assembly.resolver),
        _diagnostic_sysrq_handler_registrar(
            resolver=assembly.resolver,
            secret_registry=assembly.secret_registry,
            object_stores=assembly.object_stores,
        ),
        _watch_for_crash_handler_registrar(
            resolver=assembly.resolver, secret_registry=assembly.secret_registry
        ),
        _vmcore_handlers_registrar(assembly.resolver),
        _image_build_handler_registrar(
            resolver=assembly.resolver, object_stores=assembly.object_stores
        ),
        _register_diagnostics_handlers,
    )
