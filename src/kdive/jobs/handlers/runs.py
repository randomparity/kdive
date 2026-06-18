"""Registrar facade for the `runs.*` worker handlers."""

from __future__ import annotations

from kdive.domain.models import JobKind
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
    "ServerBuildProfile",
    "_run_build",
    "boot_handler",
    "build_handler",
    "install_handler",
    "register_handlers",
]


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    transport_factories: BuildHostTransportFactories | None = None,
    artifact_store: ObjectStore | None = None,
) -> None:
    """Bind the `build`/`install`/`boot` job handlers."""
    registry.register(
        JobKind.BUILD,
        lambda conn, job: build_handler(
            conn,
            job,
            resolver=resolver,
            secret_registry=secret_registry,
            transport_factories=transport_factories,
        ),
    )
    registry.register(
        JobKind.INSTALL,
        lambda conn, job: install_handler(conn, job, resolver=resolver),
    )
    registry.register(
        JobKind.BOOT,
        lambda conn, job: boot_handler(
            conn,
            job,
            resolver=resolver,
            secret_registry=secret_registry,
            artifact_store=artifact_store,
        ),
    )
