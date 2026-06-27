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

This module is **independent** of ``local_libvirt`` (ADR-0076: no provider-to-provider coupling);
it reuses only the neutral build-host, artifact, component-reference, and build-artifact helpers.
The slow, environment-bound
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
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.observability.build_telemetry import DISABLED_RECORDER, BuildPhaseRecorder
from kdive.profiles.build import ServerBuildProfile
from kdive.providers.ports.build_transport import BuildTransport
from kdive.providers.shared.build_host import execution as _build_exec
from kdive.providers.shared.build_host.configuration import config as _build_config
from kdive.providers.shared.build_host.orchestration import BuildHostOrchestrator, WorkspaceCleanup
from kdive.providers.shared.build_host.pipeline import (
    BuildArtifactPipeline,
    ReadArtifactSource,
    StagingCleanup,
    StagingFactory,
)
from kdive.providers.shared.build_host.publishing.artifact_publish import (
    ArtifactBytes,
    ArtifactRemoteFile,
    ArtifactSource,
    StorePort,
)
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

_TENANT = "remote-libvirt"
_RETENTION_CLASS = "build"
_SENSITIVITY = Sensitivity.SENSITIVE


def _local_staging_cleanup(mod_root: Path) -> None:
    """Worker-local staging cleanup: ``shutil.rmtree`` the worker-side module-staging dir."""
    shutil.rmtree(mod_root, ignore_errors=True)


class RemoteLibvirtBuild:
    """The realized remote Build port: worker ``make`` + one vmlinuz+modules bundle (ADR-0081).

    The Build port methods delegate to ``BuildArtifactPipeline`` and ``BuildHostOrchestrator``.
    """

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
        read_vmlinux_source: ReadArtifactSource,
        read_build_id: _build_exec.ReadBuildId,
        staging_factory: StagingFactory,
        catalog_fetch: CatalogConfigFetch,
        allowed_component_roots: list[Path] | None = None,
        staging_cleanup: StagingCleanup = _local_staging_cleanup,
        workspace_cleanup: WorkspaceCleanup | None = None,
        sandbox_provider: SandboxProvider | None = None,
    ) -> None:
        self._sandbox_provider = sandbox_provider
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
        self._pipeline = BuildArtifactPipeline(
            orchestrator=self._orchestrator,
            tenant=_TENANT,
            store_factory=store_factory,
            run_modules_install=run_modules_install,
            make_bundle=make_bundle,
            read_vmlinux_source=read_vmlinux_source,
            read_build_id=read_build_id,
            staging_factory=staging_factory,
            staging_cleanup=staging_cleanup,
            sensitivity=_SENSITIVITY,
            retention_class=_RETENTION_CLASS,
            staging_owner=self._own_staging_for_sandbox,
        )
        self._store_factory = store_factory
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
        sandbox_provider = resolve_build_sandbox_provider()
        return cls(
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_build_workspace.make_checkout(
                kernel_src, secret_registry, sandbox_provider=sandbox_provider
            ),
            run_olddefconfig=lambda ws: _build_exec.real_run_olddefconfig(
                ws, sandbox=sandbox_provider.get(), registry=secret_registry
            ),
            read_config=_build_exec.real_read_config,
            run_make=lambda ws: _build_exec.real_run_make(
                ws, sandbox=sandbox_provider.get(), registry=secret_registry
            ),
            run_modules_install=lambda ws, mr: _build_exec.real_run_modules_install(
                ws, mr, sandbox=sandbox_provider.get(), registry=secret_registry
            ),
            make_bundle=local_kernel_bundle,
            read_vmlinux_source=_local_vmlinux_source,
            read_build_id=_build_exec.real_read_build_id,
            staging_factory=_real_staging_factory,
            catalog_fetch=build_config_fetch_from_env(),
            allowed_component_roots=allowed_component_roots,
            sandbox_provider=sandbox_provider,
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
            run_olddefconfig=transport_run_olddefconfig(transport, secret_registry),
            read_config=transport_read_config(transport),
            run_make=transport_run_make(transport, secret_registry),
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
        return self._pipeline.build(run_id, profile, recorder=recorder, provider=provider)

    def validate_config_ref(self, ref: ComponentRef) -> None:
        self._orchestrator.validate_config_ref(ref)

    def publish(self, run_id: UUID, name: str, source: ArtifactSource) -> StoredArtifact:
        return self._pipeline.publish(run_id, name, source)

    def _own_staging_for_sandbox(self, mod_root: Path) -> None:
        sandbox = self._sandbox_provider.get() if self._sandbox_provider is not None else None
        if sandbox is not None:
            sandbox.own(mod_root)


def _local_vmlinux_source(workspace: Path) -> ArtifactSource:  # pragma: no cover - live_vm
    """Worker-local vmlinux seam: read ``vmlinux`` into memory as :class:`ArtifactBytes`."""
    return ArtifactBytes(_build_exec.real_read_vmlinux(workspace))


def transport_vmlinux_source(t: BuildTransport) -> ReadArtifactSource:
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
    "RemoteLibvirtBuild",
    "transport_vmlinux_source",
]
