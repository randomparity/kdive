"""Registrar facade for the `runs.*` worker handlers."""

from __future__ import annotations

from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.runs.boot import boot_handler
from kdive.jobs.handlers.runs.install import install_handler
from kdive.jobs.handlers.runs.ports import RunHandlerPorts
from kdive.jobs.models import HandlerRegistry

__all__ = [
    "RunHandlerPorts",
    "boot_handler",
    "install_handler",
    "register_handlers",
]


def register_handlers(
    registry: HandlerRegistry,
    *,
    ports: RunHandlerPorts,
) -> None:
    """Bind the `install`/`boot` job handlers."""
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
