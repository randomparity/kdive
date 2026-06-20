"""Dispatch a build onto the selected build-host transport."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from uuid import UUID

from kdive.build_artifacts.results import BuildOutput
from kdive.db.build_host_policy import check_warm_tree_source_admission
from kdive.db.build_hosts import BuildHost, BuildHostKind
from kdive.domain.build_phase import BuildPhase
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.build_telemetry import DISABLED_RECORDER, BuildPhaseRecorder
from kdive.profiles.build import GitKernelSource, GitSourceRef, ServerBuildProfile, is_git_source
from kdive.providers.ports import Builder, TransportCapableBuilder
from kdive.providers.ports.build_transport import BuildTransport
from kdive.providers.shared.build_host.transports.ssh_transport import SshBuildTransport
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
    kernel_src: str,
    transport_factories: BuildHostTransportFactories | None = None,
    recorder: BuildPhaseRecorder = DISABLED_RECORDER,
    provider: str = "",
) -> BuildOutput:
    """Run ``builder`` on the selected build host.

    For a ``LOCAL`` **warm-tree** build the ``KDIVE_KERNEL_SRC`` (``kernel_src``, resolved by
    the worker BUILD handler) is admitted before the build runs (ADR-0161), so an
    unset/invalid tree fails before any workspace side effect; ``sync_tree`` keeps the
    backstop. The admission runs off the event loop because its usability probe stats
    the path. A ``LOCAL`` **git** build (ADR-0162) clones its allowlisted remote instead of
    mirroring the warm tree, so it does not read ``KDIVE_KERNEL_SRC`` and skips the warm-tree
    admission. ``kernel_src`` is ignored for non-``LOCAL`` (git/remote) hosts.
    """
    if host.kind is BuildHostKind.LOCAL:
        if not is_git_source(parsed):
            await asyncio.to_thread(
                check_warm_tree_source_admission, kernel_src, host_kind=host.kind
            )
        return await asyncio.to_thread(
            lambda: builder.build(run_id, parsed, recorder=recorder, provider=provider)
        )
    capable = _require_transport_capable(builder, host, run_id)
    factories = _transport_factories(transport_factories)
    factory = factories.get(host.kind)
    if factory is None:
        raise CategorizedError(
            "unsupported build host kind",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "run_id": str(run_id),
                "build_host": host.name,
                "build_host_kind": str(host.kind),
            },
        )
    # The transport session — factory __enter__ (VM provision + minutes-long synchronous
    # readiness waits, or SSH identity materialization), bind, the synchronous build, and
    # __exit__ teardown — runs entirely off the event loop in one worker thread. Entering it on
    # the loop froze the /livez heartbeat ticker and aux server so the kubelet SIGKILLed the
    # worker mid-build (#583, ADR-0181). The unsupported-kind error above is raised before any
    # offload so it stays a synchronous configuration failure.
    return await asyncio.to_thread(
        _build_over_transport_session,
        capable,
        factory,
        host=host,
        run_id=run_id,
        parsed=parsed,
        source=_git_source(parsed),
        secret_registry=secret_registry,
        recorder=recorder,
        provider=provider,
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


def _build_over_transport_session(
    builder: TransportCapableBuilder,
    factory: BuildHostTransportFactory,
    *,
    host: BuildHost,
    run_id: UUID,
    parsed: ServerBuildProfile,
    source: GitSourceRef | None,
    secret_registry: SecretRegistry,
    recorder: BuildPhaseRecorder = DISABLED_RECORDER,
    provider: str = "",
) -> BuildOutput:
    """Run the whole transport-session lifecycle synchronously (caller offloads it to a thread).

    Opens the transport ``factory`` context manager (its ``__enter__`` provisions/materializes the
    host and its ``__exit__`` tears it down), binds the builder onto the transport, and runs the
    synchronous build inside the ``with`` block. ``bind_over_transport`` is referenced by its
    module-global name so a test monkeypatching it on this module still applies inside the worker
    thread (ADR-0181).
    """
    transport_ctx = factory(host, secret_registry, run_id, source)
    with recorder.phase(BuildPhase.PROVISION, provider):
        transport = transport_ctx.__enter__()
    try:
        git_remote, git_ref = _git_coords(parsed, run_id)
        bound = bind_over_transport(
            builder,
            transport,
            host_workspace_root=host.workspace_root,
            git_remote=git_remote,
            git_ref=git_ref,
            secret_registry=secret_registry,
        )
        result = bound.build(run_id, parsed, recorder=recorder, provider=provider)
    except BaseException as exc:
        # Return value intentionally ignored: a build-transport exception must always propagate.
        # Suppression (returning True from __exit__) would silently swallow a build failure, which
        # is a worse outcome than any cleanup side effect the context manager might want to attempt.
        # This diverges from `with`-block semantics deliberately.
        transport_ctx.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        transport_ctx.__exit__(None, None, None)
    return result


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
