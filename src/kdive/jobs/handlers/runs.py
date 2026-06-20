"""Registrar facade for the `runs.*` worker handlers."""

from __future__ import annotations

from dataclasses import dataclass, field

from kdive.domain.operations.jobs import JobKind
from kdive.jobs.build_telemetry import BuildPhaseRecorder
from kdive.jobs.handlers.runs_boot import boot_handler
from kdive.jobs.handlers.runs_build import (
    BuildHostTransportFactories,
    BuildProfile,
    ServerBuildProfile,
    _run_build,
    build_handler,
)
from kdive.jobs.handlers.runs_install import install_handler
from kdive.jobs.models import HandlerRegistry
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore

__all__ = [
    "BuildProfile",
    "BuildHostTransportFactories",
    "RunHandlerPorts",
    "ServerBuildProfile",
    "_run_build",
    "boot_handler",
    "build_handler",
    "install_handler",
    "register_handlers",
]


@dataclass(frozen=True, slots=True)
class RunHandlerPorts:
    """Dependencies shared by the build, install, and boot job handlers."""

    resolver: ProviderResolver
    secret_registry: SecretRegistry
    transport_factories: BuildHostTransportFactories | None = None
    artifact_store: ObjectStore | None = None
    build_phase_recorder: BuildPhaseRecorder = field(default_factory=BuildPhaseRecorder.disabled)


def register_handlers(
    registry: HandlerRegistry,
    *,
    ports: RunHandlerPorts,
) -> None:
    """Bind the `build`/`install`/`boot` job handlers."""
    registry.register(
        JobKind.BUILD,
        lambda conn, job: build_handler(
            conn,
            job,
            resolver=ports.resolver,
            secret_registry=ports.secret_registry,
            transport_factories=ports.transport_factories,
            build_phase_recorder=ports.build_phase_recorder,
        ),
    )
    registry.register(
        JobKind.INSTALL,
        lambda conn, job: install_handler(conn, job, resolver=ports.resolver),
    )
    registry.register(
        JobKind.BOOT,
        lambda conn, job: boot_handler(
            conn,
            job,
            resolver=ports.resolver,
            secret_registry=ports.secret_registry,
            artifact_store=ports.artifact_store,
        ),
    )
