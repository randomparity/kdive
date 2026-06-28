"""Registrar facade for the `runs.*` worker handlers."""

from __future__ import annotations

from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.runs.boot import boot_handler
from kdive.jobs.handlers.runs.build import (
    BuildHostTransportFactories,
    BuildProfile,
    ServerBuildProfile,
    _run_build,
    build_handler,
)
from kdive.jobs.handlers.runs.composite import composite_handler
from kdive.jobs.handlers.runs.install import install_handler
from kdive.jobs.handlers.runs.ports import RunHandlerPorts
from kdive.jobs.models import HandlerRegistry

__all__ = [
    "BuildProfile",
    "BuildHostTransportFactories",
    "RunHandlerPorts",
    "ServerBuildProfile",
    "_run_build",
    "boot_handler",
    "build_handler",
    "composite_handler",
    "install_handler",
    "register_handlers",
]


def register_handlers(
    registry: HandlerRegistry,
    *,
    ports: RunHandlerPorts,
) -> None:
    """Bind the `build`/`install`/`boot`/`build_install_boot` job handlers."""
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
    registry.register(
        JobKind.BUILD_INSTALL_BOOT,
        lambda conn, job: composite_handler(conn, job, ports=ports),
    )
