"""Shared build-host post-checkout pipeline for kernel artifact publication."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from kdive.artifacts.storage import StoredArtifact
from kdive.build_artifacts.results import BuildOutput
from kdive.domain.build_phase import BuildPhase
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.jobs.build_telemetry import BuildPhaseRecorder
from kdive.profiles.build import ServerBuildProfile
from kdive.providers.shared.build_host import execution as _build_exec
from kdive.providers.shared.build_host.orchestration import BuildHostOrchestrator
from kdive.providers.shared.build_host.publishing.artifact_publish import (
    ArtifactSource,
    StorePort,
    publish_artifact_source,
)
from kdive.providers.shared.build_host.publishing.build_log import build_workspace_capturing_log
from kdive.providers.shared.build_host.publishing.kernel_bundle import MakeKernelBundle
from kdive.providers.shared.build_host.workspaces import workspace as _build_workspace

type ReadArtifactSource = Callable[[Path], ArtifactSource]
type StagingFactory = Callable[[], Path]
type StagingCleanup = Callable[[Path], None]
type StagingOwner = Callable[[Path], None]


@dataclass(slots=True)
class BuildArtifactPipeline:
    """Run modules_install, publish build artifacts, and clean build-host state.

    The pipeline always runs ``make modules_install`` and publishes two artifacts for the
    run: a unified ``kernel`` bundle containing ``boot/vmlinuz`` plus ``lib/modules/<ver>/``,
    and a ``vmlinux`` debuginfo artifact. Worker-local byte sources are PUT directly;
    transport-backed remote files publish through presigned PUTs so the worker does not read
    host-side artifact bytes.
    """

    orchestrator: BuildHostOrchestrator
    tenant: str
    store_factory: Callable[[], StorePort]
    run_modules_install: _build_exec.RunModulesInstall
    make_bundle: MakeKernelBundle
    read_vmlinux_source: ReadArtifactSource
    read_build_id: _build_exec.ReadBuildId
    staging_factory: StagingFactory
    staging_cleanup: StagingCleanup
    sensitivity: Sensitivity = Sensitivity.SENSITIVE
    retention_class: str = "build"
    staging_owner: StagingOwner | None = None
    _store: StorePort | None = field(default=None, init=False)

    def build(
        self,
        run_id: UUID,
        profile: ServerBuildProfile,
        *,
        recorder: BuildPhaseRecorder,
        provider: str,
    ) -> BuildOutput:
        """Build a workspace, publish kernel/vmlinux artifacts, and return their refs.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if config resolution or final
                ``.config`` validation fails; ``BUILD_FAILURE`` for non-zero build steps,
                missing artifacts, missing build-id, or host-side size/hash failures; and
                ``INFRASTRUCTURE_FAILURE`` from workspace, store, or presigned-upload IO.
        """
        workspace = self.orchestrator.workspace_path(run_id)
        try:
            workspace_result = build_workspace_capturing_log(
                lambda: self.orchestrator.build_workspace(
                    run_id, profile, recorder=recorder, provider=provider
                ),
                self._store_for_publish(),
                run_id,
                tenant=self.tenant,
            )
            mod_root = self.staging_factory()
            if self.staging_owner is not None:
                self.staging_owner(mod_root)
            try:
                kernel, vmlinux, build_id = self._publish_outputs(
                    workspace, mod_root, run_id, recorder=recorder, provider=provider
                )
            finally:
                self.staging_cleanup(mod_root)
            return _build_workspace.attach_clone_provenance(
                BuildOutput(kernel_ref=kernel.key, debuginfo_ref=vmlinux.key, build_id=build_id),
                workspace_result.clone_provenance,
            )
        finally:
            self.orchestrator.cleanup_workspace(workspace)

    def publish(self, run_id: UUID, name: str, source: ArtifactSource) -> StoredArtifact:
        """Publish one build artifact under this pipeline's provider tenant."""
        return publish_artifact_source(
            self._store_for_publish(),
            run_id,
            name,
            source,
            tenant=self.tenant,
            sensitivity=self.sensitivity,
            retention_class=self.retention_class,
        )

    def _publish_outputs(
        self,
        workspace: Path,
        mod_root: Path,
        run_id: UUID,
        *,
        recorder: BuildPhaseRecorder,
        provider: str,
    ) -> tuple[StoredArtifact, StoredArtifact, str]:
        with recorder.phase(BuildPhase.MODULES, provider):
            if self.run_modules_install(workspace, mod_root) != 0:
                raise _build_exec.build_failure("make modules_install exited non-zero", run_id)
        with recorder.phase(BuildPhase.ARTIFACT, provider):
            build_id = self.read_build_id(workspace)
            kernel = self.publish(run_id, "kernel", self.make_bundle(workspace, mod_root))
            vmlinux = self.publish(run_id, "vmlinux", self.read_vmlinux_source(workspace))
        return kernel, vmlinux, build_id

    def _store_for_publish(self) -> StorePort:
        if self._store is None:
            self._store = self.store_factory()
        return self._store
