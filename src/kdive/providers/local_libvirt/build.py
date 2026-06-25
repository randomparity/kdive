"""Local-libvirt Build plane: make a kernel and store artifacts (ADR-0029/0101/0234).

`LocalLibvirtBuild` checks out a kernel source tree (warm tree + the profile's optional patch,
or — over a transport — a git clone), preflights the resolved ``.config`` for the
kdump/debuginfo prerequisites, runs ``make`` then ``make modules_install``, extracts the produced
``vmlinux``'s GNU build-id, and stores two ``sensitive`` artifacts under deterministic Run-keyed
object keys — the **combined kernel+modules bundle** (`kernel`, a gzip tar of ``boot/vmlinuz`` +
``lib/modules/<ver>/``) and the ``vmlinux``/debuginfo (`vmlinux`). It returns both keys plus the
build-id (:class:`BuildOutput`). This is the same single artifact shape remote-libvirt produces;
both providers consume one format and there is no provider-specific carve-out (ADR-0234 §2, #766).
The install plane extracts ``boot/vmlinuz`` from the bundle host-side for the direct-kernel
``<kernel>`` element and feeds ``lib/modules/`` to its libguestfs injector.

Each artifact is produced as an :class:`ArtifactSource`: the worker-local default packages the
bundle in memory (:class:`ArtifactBytes`, PUT directly), while
:meth:`LocalLibvirtBuild.over_transport` leaves it on the build host (:class:`ArtifactRemoteFile`,
published via a presigned PUT whose checksum is computed host-side, so the worker never reads the
bytes — ADR-0101). The combined-bundle packaging is the shared :mod:`kernel_bundle` seam.

The slow, environment-bound operations are **injected seams** that default to the real
implementations, so unit tests cover the orchestration/error contract without a toolchain; the
real ``make`` path is exercised under the ``live_vm`` gate. `build()` is synchronous; the async
build handler offloads the whole call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import kdive.config as config
from kdive.artifacts.storage import StoredArtifact
from kdive.build_artifacts.results import BuildOutput
from kdive.build_configs.defaults import (
    CatalogConfigFetch,
    build_config_fetch_from_env,
)
from kdive.components.references import (
    ComponentRef,
)
from kdive.config.core_settings import BUILD_WORKSPACE, KERNEL_SRC
from kdive.domain.build_phase import BuildPhase
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.jobs.build_telemetry import DISABLED_RECORDER, BuildPhaseRecorder
from kdive.profiles.build import ServerBuildProfile
from kdive.providers.ports.build_transport import BuildTransport
from kdive.providers.shared.build_host import execution as _build_exec
from kdive.providers.shared.build_host.configuration import config as _build_config
from kdive.providers.shared.build_host.configuration.git_source import (
    local_build_remote_allowlist_from_env,
)
from kdive.providers.shared.build_host.orchestration import BuildHostOrchestrator, WorkspaceCleanup
from kdive.providers.shared.build_host.publishing.artifact_publish import (
    ArtifactBytes,
    ArtifactRemoteFile,
    ArtifactSource,
    StorePort,
    publish_artifact_source,
)
from kdive.providers.shared.build_host.publishing.build_log import build_workspace_capturing_log
from kdive.providers.shared.build_host.publishing.kernel_bundle import (
    MakeKernelBundle,
    local_kernel_bundle,
    transport_kernel_bundle,
)
from kdive.providers.shared.build_host.sandbox import (
    SandboxProvider,
    resolve_build_sandbox_provider,
)
from kdive.providers.shared.build_host.transports.transport_seams import (
    transport_git_checkout,
    transport_read_build_id,
    transport_read_config,
    transport_run_make,
    transport_run_modules_install,
    transport_run_olddefconfig,
)
from kdive.providers.shared.build_host.workspaces import workspace as _build_workspace
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_RETENTION_CLASS = "build"


type _Checkout = Callable[[UUID, ServerBuildProfile, Path, bytes], None]
type _RunOlddefconfig = _build_exec.RunStep
type _RunMake = _build_exec.RunStep
type _ReadArtifactSource = Callable[[Path], ArtifactSource]
type _StagingFactory = Callable[[], Path]
type _StagingCleanup = Callable[[Path], None]


class LocalLibvirtBuild:
    """The realized Build port: ``make`` + a combined kernel+modules bundle (ADR-0234 §2)."""

    def __init__(
        self,
        *,
        tenant: str,
        workspace_root: Path,
        store_factory: Callable[[], StorePort],
        checkout: _Checkout,
        run_olddefconfig: _RunOlddefconfig,
        read_config: _build_exec.ReadConfig,
        run_make: _RunMake,
        make_bundle: MakeKernelBundle,
        read_vmlinux_source: _ReadArtifactSource,
        read_build_id: _build_exec.ReadBuildId,
        run_modules_install: _build_exec.RunModulesInstall,
        staging_factory: _StagingFactory,
        staging_cleanup: _StagingCleanup,
        secret_registry: SecretRegistry,
        catalog_fetch: CatalogConfigFetch,
        allowed_component_roots: list[Path] | None = None,
        workspace_cleanup: WorkspaceCleanup | None = None,
        sandbox_provider: SandboxProvider | None = None,
        build_provenance_sink: dict[str, str] | None = None,
    ) -> None:
        self._tenant = tenant
        self._sandbox_provider = sandbox_provider
        self._build_provenance_sink = build_provenance_sink
        self._workspace_root = workspace_root
        self._allowed_component_roots = allowed_component_roots or [
            Path(_build_config.DEFAULT_BUILD_COMPONENT_ROOT)
        ]
        self._orchestrator = BuildHostOrchestrator.create(
            workspace_root=workspace_root,
            catalog_fetch=catalog_fetch,
            checkout=checkout,
            run_olddefconfig=run_olddefconfig,
            read_config=read_config,
            run_make=run_make,
            allowed_component_roots=self._allowed_component_roots,
            cleanup=workspace_cleanup,
        )
        self._store_factory = store_factory
        self._store: StorePort | None = None
        self._make_bundle = make_bundle
        self._read_vmlinux_source = read_vmlinux_source
        self._read_build_id = read_build_id
        self._run_modules_install = run_modules_install
        self._staging_factory = staging_factory
        self._staging_cleanup = staging_cleanup
        self._secret_registry = secret_registry
        self._catalog_fetch = catalog_fetch

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtBuild:
        """Build from the ``KDIVE_*`` environment; does not spawn ``make`` or connect S3.

        Reads the workspace root (``KDIVE_BUILD_WORKSPACE``) and the warm source tree
        (``KDIVE_KERNEL_SRC``). The object store is built lazily from the ``KDIVE_S3_*``
        env on the first ``build()``, so the worker registers its handler without S3 env
        present. The seams default to the real subprocess/ELF implementations, which run
        only when ``build()`` is called.
        """
        workspace_root = Path(config.require(BUILD_WORKSPACE))
        kernel_src = config.require(KERNEL_SRC)
        allowed_component_roots = _build_config.build_component_roots_from_env()
        sandbox_provider = resolve_build_sandbox_provider()
        # The worker-local git lane (ADR-0162) records {remote, ref, resolved_commit} here; the
        # build handler reads it back off BuildOutput and dispatch adds build_host (#778).
        build_provenance_sink: dict[str, str] = {}
        return cls(
            tenant="local",
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_build_workspace.make_checkout(
                kernel_src,
                secret_registry,
                allowlist=local_build_remote_allowlist_from_env(),
                sandbox_provider=sandbox_provider,
                provenance_sink=build_provenance_sink,
            ),
            run_olddefconfig=lambda ws: _build_exec.real_run_olddefconfig(
                ws, sandbox=sandbox_provider.get()
            ),
            read_config=_build_exec.real_read_config,
            run_make=lambda ws: _build_exec.real_run_make(ws, sandbox=sandbox_provider.get()),
            make_bundle=local_kernel_bundle,
            read_vmlinux_source=_local_vmlinux_source,
            read_build_id=_build_exec.real_read_build_id,
            run_modules_install=lambda ws, mr: _build_exec.real_run_modules_install(
                ws, mr, sandbox=sandbox_provider.get()
            ),
            staging_factory=_real_staging_factory,
            staging_cleanup=lambda p: shutil.rmtree(p, ignore_errors=True),
            catalog_fetch=build_config_fetch_from_env(),
            allowed_component_roots=allowed_component_roots,
            secret_registry=secret_registry,
            sandbox_provider=sandbox_provider,
            build_provenance_sink=build_provenance_sink,
        )

    def over_transport(
        self,
        transport: BuildTransport,
        *,
        host_workspace_root: str,
        git_remote: str,
        git_ref: str,
        secret_registry: SecretRegistry,
        provenance_sink: dict[str, str] | None = None,
    ) -> LocalLibvirtBuild:
        """Return a sibling builder whose build runs ON ``transport``'s host (ADR-0101).

        Every build step — git checkout, ``olddefconfig``, ``.config`` read, ``make``, build-id,
        and — when the resolved ``.config`` is crash-dump-capable — ``modules_install`` and the
        modules bundle — runs over ``transport`` on the build host, while the worker-side
        config/store of ``self`` (the catalog fetch, object-store factory, tenant, and
        component-root allowlist) is reused so config-fragment resolution and the presigned
        publish stay worker-side. Artifacts are published from the host via presigned PUT.

        Args:
            transport: A ready :class:`BuildTransport` (e.g. an SSH transport with a live
                identity) that runs every build step on the build host.
            host_workspace_root: Absolute path on the build host under which the per-run clone is
                created.
            git_remote: Git remote to clone on the host.
            git_ref: Git ref (tag, branch, or commit SHA) to check out on the host.
            secret_registry: Registry passed to the git-checkout seam for error redaction.

        Returns:
            A new :class:`LocalLibvirtBuild` bound to ``transport``.
        """
        host_root = Path(host_workspace_root)
        return LocalLibvirtBuild(
            tenant=self._tenant,
            workspace_root=host_root,
            store_factory=self._store_factory,
            checkout=transport_git_checkout(
                transport, git_remote, git_ref, secret_registry, provenance_sink=provenance_sink
            ),
            run_olddefconfig=transport_run_olddefconfig(transport),
            read_config=transport_read_config(transport),
            run_make=transport_run_make(transport),
            make_bundle=transport_kernel_bundle(transport),
            read_vmlinux_source=lambda ws: ArtifactRemoteFile(str(ws / "vmlinux"), transport),
            read_build_id=transport_read_build_id(transport),
            run_modules_install=transport_run_modules_install(transport),
            staging_factory=lambda: host_root / "modroot",
            staging_cleanup=lambda p: transport.cleanup(str(p)),
            secret_registry=secret_registry,
            catalog_fetch=self._catalog_fetch,
            allowed_component_roots=self._allowed_component_roots,
            workspace_cleanup=lambda ws: transport.cleanup(str(ws)),
        )

    def build(
        self,
        run_id: UUID,
        profile: ServerBuildProfile,
        *,
        recorder: BuildPhaseRecorder = DISABLED_RECORDER,
        provider: str = "",
    ) -> BuildOutput:
        """Build a kernel and store the combined ``kernel`` bundle + ``vmlinux``; return refs + id.

        Always runs ``make modules_install`` and packages ``boot/vmlinuz`` + ``lib/modules/<ver>/``
        into one ``kernel`` bundle (the unified artifact shape, ADR-0234 §2). The demoted
        ``modules_install`` writes into a build-user-owned staging dir under the sandbox (ADR-0214).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the resolved ``.config`` omits a
                kdump/debuginfo prerequisite (checked before ``make``); ``BUILD_FAILURE``
                on a non-zero ``make``/``modules_install`` exit, a missing bzImage, or a missing
                build-id; ``INFRASTRUCTURE_FAILURE`` propagated from a failed artifact store.
        """
        # `from_env` runs once per worker process, so this builder (and its checkout closure's
        # provenance_sink) is reused across every build the worker handles. Clear the sink up
        # front — BEFORE the checkout fills it — so a build only ever attaches provenance its own
        # clone recorded; a clone that records nothing must not inherit the prior build's (#778).
        if self._build_provenance_sink is not None:
            self._build_provenance_sink.clear()
        workspace = self._orchestrator.workspace_path(run_id)
        try:
            build_workspace_capturing_log(
                lambda: self._orchestrator.build_workspace(
                    run_id, profile, recorder=recorder, provider=provider
                ),
                self._store_for_publish(),
                run_id,
                tenant=self._tenant,
            )
            mod_root = self._staging_factory()
            sandbox = self._sandbox_provider.get() if self._sandbox_provider is not None else None
            if sandbox is not None:
                sandbox.own(mod_root)  # demoted modules_install writes a build-user dir (ADR-0214)
            try:
                with recorder.phase(BuildPhase.MODULES, provider):
                    if self._run_modules_install(workspace, mod_root) != 0:
                        raise _build_exec.build_failure(
                            "make modules_install exited non-zero", run_id
                        )
                with recorder.phase(BuildPhase.ARTIFACT, provider):
                    build_id = self._read_build_id(workspace)
                    kernel = self.publish(run_id, "kernel", self._make_bundle(workspace, mod_root))
                    vmlinux = self.publish(run_id, "vmlinux", self._read_vmlinux_source(workspace))
            finally:
                self._staging_cleanup(mod_root)
            return _build_workspace.attach_clone_provenance(
                BuildOutput(
                    kernel_ref=kernel.key,
                    debuginfo_ref=vmlinux.key,
                    build_id=build_id,
                ),
                self._build_provenance_sink,
            )
        finally:
            self._orchestrator.cleanup_workspace(workspace)

    def validate_config_ref(self, ref: ComponentRef) -> None:
        """Validate a build config ref's shape at run-creation (local path or catalog kind).

        A ``local`` ref is resolved against the provider roots; a ``catalog`` ref is accepted by
        kind (its existence is checked when the build fetches it, since this seam owns no DB
        connection). Any other kind is a ``CONFIGURATION_ERROR``.
        """
        self._orchestrator.validate_config_ref(ref)

    def _store_for_publish(self) -> StorePort:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    def publish(self, run_id: UUID, name: str, source: ArtifactSource) -> StoredArtifact:
        """Publish one build artifact; bytes PUT directly, host files via presigned PUT.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` propagated from a failed store
                operation or presigned upload; ``BUILD_FAILURE`` if the host-side hash/size of a
                remote file cannot be read.
        """
        return publish_artifact_source(
            self._store_for_publish(),
            run_id,
            name,
            source,
            tenant=self._tenant,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class=_RETENTION_CLASS,
        )


def _local_vmlinux_source(workspace: Path) -> ArtifactSource:  # pragma: no cover - live_vm
    """Worker-local vmlinux seam: read ``vmlinux`` into memory as :class:`ArtifactBytes`."""
    return ArtifactBytes(_build_exec.real_read_vmlinux(workspace))


def _real_staging_factory() -> Path:  # pragma: no cover - live_vm
    import tempfile

    return Path(tempfile.mkdtemp(prefix="kdive-mod-"))


__all__ = [
    "LocalLibvirtBuild",
]
