"""Remote-libvirt Build plane: worker ``make`` + a single vmlinuz+modules bundle (ADR-0081).

`RemoteLibvirtBuild` runs the kernel build on the **worker** exactly as `local_libvirt` does
— warm-tree checkout (rsync + staged ``.config`` + optional patch), ``make olddefconfig``, a
kdump/debuginfo ``.config`` preflight, ``make`` — then runs ``make modules_install`` and
publishes **one gzip-compressed install bundle** (`boot/vmlinuz` + `lib/modules/<ver>/…`) as
``kernel_ref`` plus the ``vmlinux`` debuginfo as ``debuginfo_ref``, recording the GNU
build-id. This leaves ``BuildOutput``, the ``Builder`` port, and the ``runs`` ledger
unchanged: the remote target is a disk-image base OS that installs the kernel **in-guest**
(ADR-0078), which needs the kernel's ``/lib/modules`` tree that local's direct-kernel boot
never required — so the modules travel inside the existing ``kernel_ref`` object rather than
as a third ref (which would need a port change or core DDL beyond migration 0020).

The post-``make`` pipeline (modules_install → build-id → bundle → vmlinux → publish) runs
through **injected seams** that produce an :class:`ArtifactSource`. The worker-local default
packages the bundle in memory and publishes via :meth:`ObjectStore.put_artifact` — byte-for-byte
the historical behavior. The transport-backed seams (ADR-0099) produce the artifacts on a
build host and publish each via a presigned PUT whose checksum is computed on the host, so the
worker never reads the large bundle/vmlinux bytes (it only sees the host-computed sha256).

This module is **independent** of ``local_libvirt`` (ADR-0076: no shared layer with the
provider headed for removal); it reuses only the neutral artifact, component-reference, and
build-artifact helpers and duplicates the build mechanics. The slow, environment-bound
operations are **injected seams** that default to the real implementations, so unit tests
cover the orchestration/error contract without a toolchain; the real ``make`` path is
exercised under the ``live_vm`` gate. `build()` is synchronous; the async build handler
offloads the whole call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import shutil
import tempfile
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
from kdive.components.references import ComponentRef
from kdive.config.core_settings import BUILD_WORKSPACE, KERNEL_SRC
from kdive.domain.build_phase import BuildPhase
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.jobs.build_telemetry import DISABLED_RECORDER, BuildPhaseRecorder
from kdive.profiles.build import ServerBuildProfile
from kdive.providers.ports.build_transport import BuildTransport
from kdive.providers.shared.build_host import execution as _build_exec
from kdive.providers.shared.build_host.configuration import config as _build_config
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

_TENANT = "remote-libvirt"
_RETENTION_CLASS = "build"
_SENSITIVITY = Sensitivity.SENSITIVE


type _ReadVmlinuxSource = Callable[[Path], ArtifactSource]
type _StagingFactory = Callable[[], Path]
type _StagingCleanup = Callable[[Path], None]


def _local_staging_cleanup(mod_root: Path) -> None:
    """Worker-local staging cleanup: ``shutil.rmtree`` the worker-side module-staging dir."""
    shutil.rmtree(mod_root, ignore_errors=True)


class RemoteLibvirtBuild:
    """The realized remote Build port: worker ``make`` + one vmlinuz+modules bundle (ADR-0081)."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        store_factory: Callable[[], StorePort],
        checkout: _build_workspace.Checkout,
        run_olddefconfig: _build_exec.RunStep,
        read_config: _build_exec.ReadConfig,
        run_make: _build_exec.RunStep,
        run_modules_install: _build_exec.RunModulesInstall,
        make_bundle: MakeKernelBundle,
        read_vmlinux_source: _ReadVmlinuxSource,
        read_build_id: _build_exec.ReadBuildId,
        staging_factory: _StagingFactory,
        catalog_fetch: CatalogConfigFetch,
        allowed_component_roots: list[Path] | None = None,
        staging_cleanup: _StagingCleanup = _local_staging_cleanup,
        workspace_cleanup: WorkspaceCleanup | None = None,
    ) -> None:
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
        self._run_modules_install = run_modules_install
        self._make_bundle = make_bundle
        self._read_vmlinux_source = read_vmlinux_source
        self._read_build_id = read_build_id
        self._staging_factory = staging_factory
        self._staging_cleanup = staging_cleanup
        self._catalog_fetch = catalog_fetch

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtBuild:
        """Build from the shared ``KDIVE_*`` worker build env; does not spawn ``make`` or S3.

        Reads the worker's build-host config — the workspace root (``KDIVE_BUILD_WORKSPACE``),
        the warm source tree (``KDIVE_KERNEL_SRC``), and the component roots
        (``KDIVE_BUILD_COMPONENT_ROOTS``) — the same vars ``local_libvirt`` reads; they
        describe the worker, not the provider. The object store is built lazily from the
        ``KDIVE_S3_*`` env on the first ``build()``, and the seams default to the real
        subprocess/ELF implementations, which run only when ``build()`` is called.
        """
        workspace_root = Path(config.require(BUILD_WORKSPACE))
        kernel_src = config.require(KERNEL_SRC)
        allowed_component_roots = _build_config.build_component_roots_from_env()
        return cls(
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_build_workspace.make_checkout(kernel_src, secret_registry),
            run_olddefconfig=_build_exec.real_run_olddefconfig,
            read_config=_build_exec.real_read_config,
            run_make=_build_exec.real_run_make,
            run_modules_install=_build_exec.real_run_modules_install,
            make_bundle=local_kernel_bundle,
            read_vmlinux_source=_local_vmlinux_source,
            read_build_id=_build_exec.real_read_build_id,
            staging_factory=_real_staging_factory,
            catalog_fetch=build_config_fetch_from_env(),
            allowed_component_roots=allowed_component_roots,
        )

    def over_transport(
        self,
        transport: BuildTransport,
        *,
        host_workspace_root: str,
        git_remote: str,
        git_ref: str,
        secret_registry: SecretRegistry,
    ) -> RemoteLibvirtBuild:
        """Return a sibling builder whose build runs ON ``transport``'s host (ADR-0099).

        Every build step — git checkout, ``olddefconfig``, ``.config`` read, ``make``,
        ``modules_install``, build-id, bundle, ``vmlinux`` — runs over ``transport`` on the
        build host, while the worker-side config/store of ``self`` (the catalog fetch, object
        store factory, and component-root allowlist) is reused so config-fragment resolution and
        the presigned publish stay worker-side. The module-staging tree lives under
        ``host_workspace_root`` on the host and is reclaimed via :meth:`BuildTransport.cleanup`.

        Args:
            transport: A ready :class:`BuildTransport` (e.g. an SSH transport with a live
                identity) that runs every build step on the build host.
            host_workspace_root: Absolute path on the build host under which the per-run clone
                and the module-staging tree are created.
            git_remote: Git remote to clone on the host.
            git_ref: Git ref (tag, branch, or commit SHA) to check out on the host.
            secret_registry: Registry passed to the git-checkout seam for error redaction.

        Returns:
            A new :class:`RemoteLibvirtBuild` bound to ``transport``.
        """
        host_root = Path(host_workspace_root)
        mod_root = host_root / "modroot"
        return RemoteLibvirtBuild(
            workspace_root=host_root,
            store_factory=self._store_factory,
            checkout=transport_git_checkout(transport, git_remote, git_ref, secret_registry),
            run_olddefconfig=transport_run_olddefconfig(transport),
            read_config=transport_read_config(transport),
            run_make=transport_run_make(transport),
            run_modules_install=transport_run_modules_install(transport),
            make_bundle=transport_kernel_bundle(transport),
            read_vmlinux_source=transport_vmlinux_source(transport),
            read_build_id=transport_read_build_id(transport),
            staging_factory=lambda: mod_root,
            catalog_fetch=self._catalog_fetch,
            allowed_component_roots=self._allowed_component_roots,
            staging_cleanup=lambda path: transport.cleanup(str(path)),
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
        """Build a kernel, publish a vmlinuz+modules bundle + debuginfo; return refs + build-id.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the resolved ``.config`` omits a
                kdump/debuginfo prerequisite (checked before ``make``); ``BUILD_FAILURE`` on a
                non-zero ``make``/``olddefconfig``/``modules_install`` exit or a missing
                build-id; ``INFRASTRUCTURE_FAILURE`` propagated from a failed artifact store.
        """
        workspace = self._orchestrator.workspace_path(run_id)
        try:
            build_workspace_capturing_log(
                lambda: self._orchestrator.build_workspace(
                    run_id, profile, recorder=recorder, provider=provider
                ),
                self._store_for_publish(),
                run_id,
                tenant=_TENANT,
            )
            mod_root = self._staging_factory()
            try:
                with recorder.phase(BuildPhase.MODULES, provider):
                    if self._run_modules_install(workspace, mod_root) != 0:
                        raise _build_exec.build_failure(
                            "make modules_install exited non-zero", run_id
                        )
                with recorder.phase(BuildPhase.ARTIFACT, provider):
                    build_id = self._read_build_id(workspace)
                    kernel_source = self._make_bundle(workspace, mod_root)
                    vmlinux_source = self._read_vmlinux_source(workspace)
                    kernel = self.publish(run_id, "kernel", kernel_source)
                    vmlinux = self.publish(run_id, "vmlinux", vmlinux_source)
            finally:
                self._staging_cleanup(mod_root)
            return BuildOutput(kernel_ref=kernel.key, debuginfo_ref=vmlinux.key, build_id=build_id)
        finally:
            self._orchestrator.cleanup_workspace(workspace)

    def validate_config_ref(self, ref: ComponentRef) -> None:
        """Validate a build config ref's shape at run-creation (local path or catalog kind).

        A ``local`` ref is resolved against the provider roots; a ``catalog`` ref is accepted by
        kind (its existence is checked when the build fetches it, since this seam owns no DB
        connection). Any other kind is a ``CONFIGURATION_ERROR``.
        """
        self._orchestrator.validate_config_ref(ref)

    def publish(self, run_id: UUID, name: str, source: ArtifactSource) -> StoredArtifact:
        """Publish one build artifact under ``runs/<run_id>/<name>`` and return its row.

        An :class:`ArtifactBytes` source is PUT directly from worker memory (the historical
        path). An :class:`ArtifactRemoteFile` source is published via a presigned PUT whose
        checksum is computed on the build host, so the worker never reads the file's bytes.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` propagated from a failed store
                operation or presigned upload.
        """
        return publish_artifact_source(
            self._store_for_publish(),
            run_id,
            name,
            source,
            tenant=_TENANT,
            sensitivity=_SENSITIVITY,
            retention_class=_RETENTION_CLASS,
        )

    def _store_for_publish(self) -> StorePort:
        if self._store is None:
            self._store = self._store_factory()
        return self._store


def _local_vmlinux_source(workspace: Path) -> ArtifactSource:  # pragma: no cover - live_vm
    """Worker-local vmlinux seam: read ``vmlinux`` into memory as :class:`ArtifactBytes`."""
    return ArtifactBytes(_build_exec.real_read_vmlinux(workspace))


def transport_vmlinux_source(t: BuildTransport) -> _ReadVmlinuxSource:
    """Return a ``_ReadVmlinuxSource`` yielding the host-resident ``vmlinux`` debuginfo.

    The returned seam never reads ``vmlinux``; it points an :class:`ArtifactRemoteFile` at
    ``<workspace>/vmlinux`` so :meth:`RemoteLibvirtBuild.publish` uploads it via a presigned
    PUT, hashing it on the host.

    Args:
        t: The build transport that can hash and upload the file.

    Returns:
        A callable ``(workspace: Path) -> ArtifactRemoteFile`` matching ``_ReadVmlinuxSource``.
    """

    def _source(workspace: Path) -> ArtifactSource:
        return ArtifactRemoteFile(path=str(workspace / "vmlinux"), transport=t)

    return _source


def _real_staging_factory() -> Path:  # pragma: no cover - live_vm
    return Path(tempfile.mkdtemp(prefix="kdive-mod-"))


__all__ = [
    "ArtifactBytes",
    "ArtifactRemoteFile",
    "ArtifactSource",
    "RemoteLibvirtBuild",
    "transport_vmlinux_source",
]
