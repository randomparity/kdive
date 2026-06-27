"""Dispatch a build onto the selected build-host transport."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from uuid import UUID

from kdive.build_artifacts.provenance import rev_parse_head, staged_tree_sha, working_tree_dirty
from kdive.build_artifacts.results import BuildOutput
from kdive.db.build_host_policy import check_warm_tree_source_admission
from kdive.db.build_hosts import BuildHost, BuildHostKind
from kdive.domain.build_phase import BuildPhase
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.observability.build_telemetry import DISABLED_RECORDER, BuildPhaseRecorder
from kdive.profiles.build import GitKernelSource, GitSourceRef, ServerBuildProfile, is_git_source
from kdive.providers.ports.build import (
    Builder,
    TransportCapableBuilder,
)
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


@dataclass(frozen=True, slots=True)
class BuildHostDispatchRequest:
    """Stable inputs for running one build on one admitted build host."""

    builder: Builder
    host: BuildHost
    run_id: UUID
    parsed: ServerBuildProfile
    secret_registry: SecretRegistry
    kernel_src: str
    provider: str = ""


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
    request: BuildHostDispatchRequest,
    *,
    transport_factories: BuildHostTransportFactories | None = None,
    recorder: BuildPhaseRecorder = DISABLED_RECORDER,
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
    if request.host.kind is BuildHostKind.LOCAL:
        warm_tree = not is_git_source(request.parsed)
        if warm_tree:
            await asyncio.to_thread(
                check_warm_tree_source_admission,
                request.kernel_src,
                host_kind=request.host.kind,
            )
        result = await asyncio.to_thread(
            lambda: request.builder.build(
                request.run_id,
                request.parsed,
                recorder=recorder,
                provider=request.provider,
            )
        )
        if warm_tree:
            return _with_warm_tree_provenance(result, request.parsed, request.kernel_src)
        return _with_local_git_build_host(result, request.host)
    capable = _require_transport_capable(request.builder, request.host, request.run_id)
    factories = _transport_factories(transport_factories)
    factory = factories.get(request.host.kind)
    if factory is None:
        raise CategorizedError(
            "unsupported build host kind",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "run_id": str(request.run_id),
                "build_host": request.host.name,
                "build_host_kind": str(request.host.kind),
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
        host=request.host,
        run_id=request.run_id,
        parsed=request.parsed,
        source=_git_source(request.parsed),
        secret_registry=request.secret_registry,
        recorder=recorder,
        provider=request.provider,
    )


def _with_local_git_build_host(result: BuildOutput, host: BuildHost) -> BuildOutput:
    """Add ``build_host`` to the worker-local git lane's clone provenance (ADR-0162, #778).

    The builder's checkout seam (``clone_tree``) already filled ``build_provenance`` with
    ``{remote, ref, resolved_commit}`` (remote userinfo-stripped); the build host is known here, not
    in the seam, so it is added last — symmetric to :func:`_with_git_provenance` on the transport
    path. A ``None`` provenance (the clone recorded nothing) is left untouched.
    """
    if result.build_provenance is None:
        return result
    return result._replace(build_provenance={**result.build_provenance, "build_host": host.name})


def _with_warm_tree_provenance(
    result: BuildOutput, parsed: ServerBuildProfile, kernel_src: str
) -> BuildOutput:
    """Attach best-effort ``{label, resolved_commit, dirty, tree_sha?}`` provenance (#778, #861).

    A warm-tree build rsyncs ``$KDIVE_KERNEL_SRC`` **working-tree state** (uncommitted edits and
    untracked files included), not ``HEAD``, and the bare ``kernel_source_ref`` is a decorative
    label, not a remote. The label is always recorded. When the staged tree is a git work tree
    (``rev-parse HEAD`` succeeds) the HEAD it is based on is recorded as ``resolved_commit``
    (decorative when dirty), plus ``dirty`` (does the tree differ from HEAD) and, for a dirty tree
    with tracked changes, a content-deterministic ``tree_sha`` of the tracked working-tree state
    (ADR-0265). Each probe is best-effort: a failed probe omits its key and never fails the build,
    so a non-git tree degrades to ``{label}``. The probes read the live staged tree at
    build-completion (the existing ``resolved_commit`` timing), and ``dirty``/``tree_sha`` cover
    git-tracked content only.
    """
    label = parsed.kernel_source_ref
    if not isinstance(label, str):
        return result
    provenance: dict[str, str | bool] = {"label": label}
    commit = rev_parse_head(kernel_src)
    if commit is not None:
        provenance["resolved_commit"] = commit
        dirty = working_tree_dirty(kernel_src)
        if dirty is not None:
            provenance["dirty"] = dirty
            if dirty:
                tree_sha = staged_tree_sha(kernel_src)
                if tree_sha is not None:
                    provenance["tree_sha"] = tree_sha
    return result._replace(build_provenance=provenance)


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
    return _with_git_provenance(result, host)


def _with_git_provenance(result: BuildOutput, host: BuildHost) -> BuildOutput:
    """Attach ``{**clone-provenance, build_host}`` to ``result`` when the clone recorded any.

    The bound builder attaches ``{remote, ref, resolved_commit}`` (remote userinfo-stripped); the
    build host is known here, not in the checkout seam, so it is added last. ``None`` provenance
    is left untouched (#778).
    """
    if result.build_provenance is None:
        return result
    return result._replace(build_provenance={**result.build_provenance, "build_host": host.name})


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
