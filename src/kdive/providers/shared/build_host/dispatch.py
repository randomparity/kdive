"""Dispatch a build onto the selected build-host transport."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from uuid import UUID

from kdive.build_artifacts.results import BuildOutput
from kdive.db.build_hosts import BuildHost, BuildHostKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import GitKernelSource, GitSourceRef, ServerBuildProfile
from kdive.providers.ports import Builder, TransportCapableBuilder
from kdive.providers.ports.build_transport import BuildTransport
from kdive.providers.shared.build_host.ssh_transport import SshBuildTransport
from kdive.security.secrets.secret_registry import SecretRegistry

# Patchable seam: tests substitute this to avoid real SSH.
ssh_build_transport_from_host = SshBuildTransport.from_host

# The factory receives the configured git build source so a transport that provisions a throwaway
# guest (ephemeral_libvirt) can preflight egress to it before the clone (ADR-0155); ``None`` for a
# warm-tree source. The ssh/local factories ignore it (their host network is already up).
type BuildHostTransportFactory = Callable[
    [BuildHost, SecretRegistry, UUID, GitSourceRef | None], AbstractContextManager[BuildTransport]
]
type BuildHostTransportFactories = Mapping[BuildHostKind, BuildHostTransportFactory]


def ssh_build_transport_factory(
    host: BuildHost,
    secret_registry: SecretRegistry,
    _run_id: UUID,
    _source: GitSourceRef | None,
) -> AbstractContextManager[BuildTransport]:
    return ssh_build_transport_from_host(host, secret_registry)


def default_build_host_transport_factories() -> dict[BuildHostKind, BuildHostTransportFactory]:
    """Return shared build-host transport factories owned outside provider runtimes."""
    return {BuildHostKind.SSH: ssh_build_transport_factory}


async def run_build_on_host(
    builder: Builder,
    host: BuildHost,
    run_id: UUID,
    parsed: ServerBuildProfile,
    *,
    secret_registry: SecretRegistry,
    transport_factories: BuildHostTransportFactories | None = None,
) -> BuildOutput:
    """Run ``builder`` on the selected build host."""
    if host.kind is BuildHostKind.LOCAL:
        return await asyncio.to_thread(builder.build, run_id, parsed)
    capable = _require_transport_capable(builder, host, run_id)
    factories = _transport_factories(transport_factories)
    factory = factories.get(host.kind)
    if factory is not None:
        source = _git_source(parsed)
        with factory(host, secret_registry, run_id, source) as transport:
            return await _run_over_transport(
                capable,
                transport,
                host=host,
                run_id=run_id,
                parsed=parsed,
                secret_registry=secret_registry,
            )
    raise CategorizedError(
        "unsupported build host kind",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "run_id": str(run_id),
            "build_host": host.name,
            "build_host_kind": str(host.kind),
        },
    )


def _transport_factories(
    injected: BuildHostTransportFactories | None,
) -> dict[BuildHostKind, BuildHostTransportFactory]:
    factories = default_build_host_transport_factories()
    if injected is not None:
        factories.update(injected)
    return factories


def bind_over_transport(
    builder: TransportCapableBuilder,
    transport: BuildTransport,
    *,
    host_workspace_root: str,
    git_remote: str,
    git_ref: str,
    secret_registry: SecretRegistry,
) -> Builder:
    """Rebind ``builder`` onto ``transport`` with the host workspace and git coordinates."""
    return builder.over_transport(
        transport,
        host_workspace_root=host_workspace_root,
        git_remote=git_remote,
        git_ref=git_ref,
        secret_registry=secret_registry,
    )


async def _run_over_transport(
    builder: TransportCapableBuilder,
    transport: BuildTransport,
    *,
    host: BuildHost,
    run_id: UUID,
    parsed: ServerBuildProfile,
    secret_registry: SecretRegistry,
) -> BuildOutput:
    git_remote, git_ref = _git_coords(parsed, run_id)
    bound = bind_over_transport(
        builder,
        transport,
        host_workspace_root=host.workspace_root,
        git_remote=git_remote,
        git_ref=git_ref,
        secret_registry=secret_registry,
    )
    return await asyncio.to_thread(bound.build, run_id, parsed)


def _git_coords(parsed: ServerBuildProfile, run_id: UUID) -> tuple[str, str]:
    source = parsed.kernel_source_ref
    if not isinstance(source, GitKernelSource):
        raise CategorizedError(
            "remote build host requires a git kernel_source_ref",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )
    return source.git.remote, source.git.ref


def _git_source(parsed: ServerBuildProfile) -> GitSourceRef | None:
    """The git source coordinates for the build-VM egress preflight; ``None`` for a warm tree.

    Unlike :func:`_git_coords` (which requires git for the actual remote clone), this is advisory:
    a non-git warm-tree source has no remote to preflight, so the factory simply skips the check.
    """
    source = parsed.kernel_source_ref
    return source.git if isinstance(source, GitKernelSource) else None


def _require_transport_capable(
    builder: Builder, host: BuildHost, run_id: UUID
) -> TransportCapableBuilder:
    if not isinstance(builder, TransportCapableBuilder):
        raise CategorizedError(
            "a remote build host requires a transport-capable builder",
            category=ErrorCategory.NOT_IMPLEMENTED,
            details={"run_id": str(run_id), "build_host": host.name},
        )
    return builder
