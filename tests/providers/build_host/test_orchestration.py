"""Workspace lifecycle on the shared BuildHostOrchestrator (ADR-0102).

The orchestrator owns the per-run workspace: it derives the path, and — new in #358 —
removes it after a terminal build via an injected best-effort ``cleanup`` seam (default
``shutil.rmtree``; ``over_transport`` injects ``BuildTransport.cleanup``). These tests drive
the path-derivation and cleanup seam directly; the create/checkout/make orchestration is
covered through the provider build tests.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.shared.build_host.execution import CapturedStep
from kdive.providers.shared.build_host.orchestration import BuildHostOrchestrator, WorkspaceCleanup

_RUN = UUID("44444444-4444-4444-4444-444444444444")


def _server_profile() -> ServerBuildProfile:
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
            "patch_ref": None,
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    return profile


def _validating_orchestrator(
    tmp_path: Path, *, fragment: bytes, final_config: str
) -> BuildHostOrchestrator:
    """An orchestrator whose preflight sees ``fragment`` requested and ``final_config`` built."""
    return BuildHostOrchestrator.create(
        workspace_root=tmp_path,
        catalog_fetch=lambda _name: fragment,
        checkout=lambda _r, _p, _w, _f: None,
        run_olddefconfig=lambda _w: CapturedStep(0, ""),
        read_config=lambda _w: final_config,
        run_make=lambda _w: CapturedStep(0, ""),
    )


def _orchestrator(
    workspace_root: Path, *, cleanup: WorkspaceCleanup | None = None
) -> BuildHostOrchestrator:
    """An orchestrator with inert build seams; only the workspace lifecycle is exercised."""
    return BuildHostOrchestrator.create(
        workspace_root=workspace_root,
        catalog_fetch=lambda _name: b"",
        checkout=lambda _r, _p, _w, _f: None,
        run_olddefconfig=lambda _w: CapturedStep(0, ""),
        read_config=lambda _w: "",
        run_make=lambda _w: CapturedStep(0, ""),
        cleanup=cleanup,
    )


def test_workspace_path_is_root_joined_with_run_id(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path / "ws")
    assert orch.workspace_path(_RUN) == tmp_path / "ws" / str(_RUN)


def test_cleanup_workspace_invokes_injected_seam_with_path(tmp_path: Path) -> None:
    seen: list[Path] = []
    orch = _orchestrator(tmp_path / "ws", cleanup=seen.append)

    workspace = orch.workspace_path(_RUN)
    orch.cleanup_workspace(workspace)

    assert seen == [workspace]


def test_default_cleanup_removes_a_real_directory(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path / "ws")
    workspace = orch.workspace_path(_RUN)
    workspace.mkdir(parents=True)
    (workspace / "vmlinux").write_bytes(b"x")

    orch.cleanup_workspace(workspace)

    assert not workspace.exists()


def test_default_cleanup_on_missing_path_does_not_raise(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path / "ws")
    orch.cleanup_workspace(orch.workspace_path(_RUN))  # never created — must be a no-op


def test_build_workspace_rejects_dropped_fragment_symbols(tmp_path: Path) -> None:
    # A fragment symbol that olddefconfig turned off must abort the build with a
    # CONFIGURATION_ERROR naming the dropped symbols.
    orch = _validating_orchestrator(
        tmp_path,
        fragment=b"CONFIG_CRASH_DUMP=y\n",
        final_config="# CONFIG_CRASH_DUMP is not set\n",
    )

    with pytest.raises(CategorizedError) as caught:
        orch.build_workspace(_RUN, _server_profile())

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert (
        str(caught.value)
        == "kdump fragment symbols were dropped by olddefconfig (unmet base dependency)"
    )
    assert caught.value.details == {"dropped": ["CONFIG_CRASH_DUMP"]}


def test_build_workspace_rejects_config_missing_required_group(tmp_path: Path) -> None:
    # No symbols are dropped, but the final .config omits a whole required group (the
    # DWARF/BTF debuginfo group), so the build must still abort.
    orch = _validating_orchestrator(
        tmp_path,
        fragment=b"CONFIG_CRASH_DUMP=y\n",
        final_config="CONFIG_CRASH_DUMP=y\n",
    )

    with pytest.raises(CategorizedError) as caught:
        orch.build_workspace(_RUN, _server_profile())

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "kernel .config omits a required kdump/debuginfo option"
    assert caught.value.details["missing_any_of"] == [
        ["CONFIG_DEBUG_INFO_DWARF4", "CONFIG_DEBUG_INFO_DWARF5", "CONFIG_DEBUG_INFO_BTF"]
    ]
