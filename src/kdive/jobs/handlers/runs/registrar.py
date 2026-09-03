"""Registrar facade for the `runs.*` worker handlers."""

from __future__ import annotations

from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.external_boot.operations import ExternalBootOperations
from kdive.jobs.handlers.external_boot.router import route_marked
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
    external_boot: ExternalBootOperations,
) -> None:
    """Bind the `install`/`boot` job handlers, diverting an authority-marked boot.

    ``external_boot`` is required rather than defaulted: a call site that omitted it would send a
    marked job to ``boot_handler``, which boots a Run. Making it required means that mistake is a
    ``TypeError`` at registration instead of a wrong operation against a live System.
    """
    registry.register(
        JobKind.INSTALL,
        lambda conn, job: install_handler(
            conn,
            job,
            resolver=ports.resolver,
            incarnation_credential=ports.incarnation_credential,
        ),
    )
    registry.register(
        JobKind.BOOT,
        route_marked(
            external_boot,
            lambda conn, job: boot_handler(
                conn,
                job,
                resolver=ports.resolver,
                secret_registry=ports.secret_registry,
                artifact_store=ports.artifact_store,
            ),
        ),
    )
