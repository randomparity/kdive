"""Provider-neutral kernel build-host orchestration."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.build_configs.defaults import DEFAULT_CONFIG_REF, CatalogConfigFetch
from kdive.components.references import ComponentRef
from kdive.components.requirements import validate_config_requirements
from kdive.domain.build_phase import BuildPhase
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.build_telemetry import DISABLED_RECORDER, BuildPhaseRecorder
from kdive.profiles.build import ServerBuildProfile
from kdive.providers.shared.build_host.common import _dropped_fragment_symbols
from kdive.providers.shared.build_host.configuration.config import (
    DEFAULT_BUILD_COMPONENT_ROOT,
    load_profile_config_requirements,
    missing_config_groups,
    resolve_config_bytes,
    validate_config_ref,
)
from kdive.providers.shared.build_host.execution import ReadConfig, RunStep, build_failure
from kdive.providers.shared.build_host.workspaces.workspace import Checkout, CloneProvenance

REQUIRED_KERNEL_CONFIG: tuple[tuple[str, ...], ...] = (
    ("CONFIG_CRASH_DUMP",),
    ("CONFIG_DEBUG_INFO_DWARF4", "CONFIG_DEBUG_INFO_DWARF5", "CONFIG_DEBUG_INFO_BTF"),
)

# Removes the per-run workspace after a terminal build. The worker-local default rmtrees the
# rsync destination; over_transport injects a transport-routed removal of the host-side clone.
type WorkspaceCleanup = Callable[[Path], None]


def _default_workspace_cleanup(workspace: Path) -> None:
    """Best-effort removal of a worker-side per-run workspace; never raises on a missing tree."""
    shutil.rmtree(workspace, ignore_errors=True)


@dataclass(slots=True)
class BuildWorkspaceResult:
    """Workspace path and any clone provenance produced during source checkout."""

    workspace: Path
    clone_provenance: CloneProvenance | None


@dataclass(slots=True)
class BuildHostOrchestrator:
    """Shared build-host config resolution, preflight, and ``make`` orchestration."""

    workspace_root: Path
    catalog_fetch: CatalogConfigFetch
    checkout: Checkout
    run_olddefconfig: RunStep
    read_config: ReadConfig
    run_make: RunStep
    allowed_component_roots: list[Path]
    cleanup: WorkspaceCleanup

    @classmethod
    def create(
        cls,
        *,
        workspace_root: Path,
        catalog_fetch: CatalogConfigFetch,
        checkout: Checkout,
        run_olddefconfig: RunStep,
        read_config: ReadConfig,
        run_make: RunStep,
        allowed_component_roots: list[Path] | None = None,
        cleanup: WorkspaceCleanup | None = None,
    ) -> BuildHostOrchestrator:
        """Build an orchestrator with the default component-root allowlist and cleanup seam."""
        return cls(
            workspace_root=workspace_root,
            catalog_fetch=catalog_fetch,
            checkout=checkout,
            run_olddefconfig=run_olddefconfig,
            read_config=read_config,
            run_make=run_make,
            allowed_component_roots=allowed_component_roots or [Path(DEFAULT_BUILD_COMPONENT_ROOT)],
            cleanup=cleanup or _default_workspace_cleanup,
        )

    def workspace_path(self, run_id: UUID) -> Path:
        """The per-run workspace path ``<workspace_root>/<run_id>`` (created by the build)."""
        return self.workspace_root / str(run_id)

    def cleanup_workspace(self, workspace: Path) -> None:
        """Remove a per-run workspace via the injected best-effort cleanup seam."""
        self.cleanup(workspace)

    def build_workspace(
        self,
        run_id: UUID,
        profile: ServerBuildProfile,
        *,
        recorder: BuildPhaseRecorder = DISABLED_RECORDER,
        provider: str = "",
    ) -> BuildWorkspaceResult:
        """Resolve config, checkout, preflight, run ``make``, and return workspace metadata."""
        workspace = self.workspace_path(run_id)
        config_ref = profile.config or DEFAULT_CONFIG_REF
        fragment_bytes = resolve_config_bytes(
            config_ref,
            allowed_component_roots=self.allowed_component_roots,
            catalog_fetch=self.catalog_fetch,
        )
        fragment_text = fragment_bytes.decode()
        with recorder.phase(BuildPhase.SOURCE_SYNC, provider):
            clone_provenance = self.checkout(run_id, profile, workspace, fragment_bytes)
        with recorder.phase(BuildPhase.CONFIGURE, provider):
            olddefconfig = self.run_olddefconfig(workspace)
            if olddefconfig.returncode != 0:
                raise build_failure(
                    "make olddefconfig exited non-zero", run_id, build_log=olddefconfig.output
                )
            config_text = self.read_config(workspace)
            _validate_final_config(run_id, profile, fragment_text, config_text)
        with recorder.phase(BuildPhase.COMPILE, provider):
            make = self.run_make(workspace)
            if make.returncode != 0:
                raise build_failure("make exited non-zero", run_id, build_log=make.output)
        return BuildWorkspaceResult(workspace=workspace, clone_provenance=clone_provenance)

    def validate_config_ref(self, ref: ComponentRef) -> None:
        """Validate a build config ref's shape at run-creation.

        ``local`` refs must resolve under the provider's allowed component roots. ``catalog``
        refs are shape-valid here; their existence is checked when a build fetches the config
        because this seam has no database connection. Other ref kinds raise
        ``CONFIGURATION_ERROR``.
        """
        validate_config_ref(ref, allowed_component_roots=self.allowed_component_roots)


def _validate_final_config(
    run_id: UUID, profile: ServerBuildProfile, fragment_text: str, config_text: str
) -> None:
    dropped = _dropped_fragment_symbols(fragment_text, config_text)
    if dropped:
        raise CategorizedError(
            "kdump fragment symbols were dropped by olddefconfig (unmet base dependency)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"dropped": dropped},
        )
    missing = missing_config_groups(config_text, REQUIRED_KERNEL_CONFIG)
    if missing:
        raise CategorizedError(
            "kernel .config omits a required kdump/debuginfo option",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing_any_of": [list(group) for group in missing]},
        )
    if profile.profile_requirements is not None:
        requirements = load_profile_config_requirements(
            provider=profile.profile_requirements.provider,
            name=profile.profile_requirements.name,
        )
        try:
            validate_config_requirements(config_text, requirements)
        except CategorizedError as exc:
            exc.details.setdefault("run_id", str(run_id))
            raise
