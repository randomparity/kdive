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
from kdive.jobs.build_telemetry import DISABLED_RECORDER, BuildPhaseRecorder
from kdive.profiles.build import ServerBuildProfile
from kdive.providers.ports.build_transport import BuildTransport
from kdive.providers.shared.build_host import execution as _build_exec
from kdive.providers.shared.build_host.configuration import config as _build_config
from kdive.providers.shared.build_host.configuration.git_source import (
    local_build_remote_allowlist_from_env,
)
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

type _Checkout = _build_workspace.Checkout
type _RunOlddefconfig = _build_exec.RunStep
type _RunMake = _build_exec.RunStep


class LocalLibvirtBuild:
    """The realized Build port: ``make`` + a combined kernel+modules bundle (ADR-0234 §2).

    The Build port methods delegate to ``BuildArtifactPipeline`` and ``BuildHostOrchestrator``.
    """

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
        read_vmlinux_source: ReadArtifactSource,
        read_build_id: _build_exec.ReadBuildId,
        run_modules_install: _build_exec.RunModulesInstall,
        staging_factory: StagingFactory,
        staging_cleanup: StagingCleanup,
        secret_registry: SecretRegistry,
        catalog_fetch: CatalogConfigFetch,
        allowed_component_roots: list[Path] | None = None,
        workspace_cleanup: WorkspaceCleanup | None = None,
        sandbox_provider: SandboxProvider | None = None,
    ) -> None:
        self._tenant = tenant
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
            tenant=tenant,
            store_factory=store_factory,
            run_modules_install=run_modules_install,
            make_bundle=make_bundle,
            read_vmlinux_source=read_vmlinux_source,
            read_build_id=read_build_id,
            staging_factory=staging_factory,
            staging_cleanup=staging_cleanup,
            staging_owner=self._own_staging_for_sandbox,
        )
        self._store_factory = store_factory
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
        return cls(
            tenant="local",
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_build_workspace.make_checkout(
                kernel_src,
                secret_registry,
                allowlist=local_build_remote_allowlist_from_env(),
                sandbox_provider=sandbox_provider,
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
        )

    def over_transport(
        self,
        transport: BuildTransport,
        *,
        host_workspace_root: str,
        git_remote: str,
        git_ref: str,
        secret_registry: SecretRegistry,
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
            checkout=transport_git_checkout(transport, git_remote, git_ref, secret_registry),
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


def _real_staging_factory() -> Path:  # pragma: no cover - live_vm
    import tempfile

    return Path(tempfile.mkdtemp(prefix="kdive-mod-"))


__all__ = [
    "LocalLibvirtBuild",
]
