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

# A final .config tail that satisfies every always-on guard: the five mount symbols,
# CONFIG_CRASH_DUMP, and one debuginfo option.
_GOOD_TAIL = (
    "CONFIG_SQUASHFS=y\nCONFIG_SQUASHFS_ZSTD=y\nCONFIG_OVERLAY_FS=y\n"
    "CONFIG_BLK_DEV_LOOP=y\nCONFIG_XFS_FS=y\nCONFIG_CRASH_DUMP=y\n"
    "CONFIG_DEBUG_INFO_DWARF5=y\n"
)


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


def test_build_workspace_rejects_missing_mount_symbol(tmp_path: Path) -> None:
    # crash-dump + debuginfo present, but a rootfs-mount symbol is missing: fails with the
    # platform reason naming the missing symbol.
    final = "CONFIG_CRASH_DUMP=y\nCONFIG_DEBUG_INFO_DWARF5=y\nCONFIG_SQUASHFS=y\n"
    orch = _validating_orchestrator(tmp_path, fragment=b"CONFIG_SQUASHFS=y\n", final_config=final)

    with pytest.raises(CategorizedError) as caught:
        orch.build_workspace(_RUN, _server_profile())

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details["reason"] == "platform_config_symbol_missing"
    missing = caught.value.details["missing"]
    assert isinstance(missing, list)
    assert "CONFIG_OVERLAY_FS" in missing


def test_build_workspace_missing_crash_dump_keeps_existing_shape(tmp_path: Path) -> None:
    # CONFIG_CRASH_DUMP stays in REQUIRED_KERNEL_CONFIG: its failure keeps missing_any_of.
    final = _GOOD_TAIL.replace("CONFIG_CRASH_DUMP=y\n", "# CONFIG_CRASH_DUMP is not set\n")
    orch = _validating_orchestrator(tmp_path, fragment=b"CONFIG_SQUASHFS=y\n", final_config=final)

    with pytest.raises(CategorizedError) as caught:
        orch.build_workspace(_RUN, _server_profile())

    assert caught.value.details["missing_any_of"] == [["CONFIG_CRASH_DUMP"]]


def test_build_workspace_accepts_a_good_final_config(tmp_path: Path) -> None:
    orch = _validating_orchestrator(
        tmp_path, fragment=b"CONFIG_SQUASHFS=y\n", final_config=_GOOD_TAIL
    )
    # Does not raise: the good tail satisfies mount + crash-dump + debuginfo.
    orch.build_workspace(_RUN, _server_profile())


def _compose_profile(names: list[str]) -> ServerBuildProfile:
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "warm-ref",
            "config": [{"kind": "catalog", "provider": "system", "name": n} for n in names],
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    return profile


def test_build_workspace_composes_two_catalog_fragments(tmp_path: Path) -> None:
    # A two-fragment compose resolves the union; the later fragment's value wins.
    fetches = {"kdump": b"CONFIG_FOO=y\n", "faultinject": b"CONFIG_FOO=m\nCONFIG_FAULT=y\n"}
    seen: list[bytes] = []
    orchestrator = BuildHostOrchestrator.create(
        workspace_root=tmp_path,
        catalog_fetch=lambda name: fetches[name],
        checkout=lambda _r, _p, _w, fragment: seen.append(fragment),
        run_olddefconfig=lambda _w: CapturedStep(0, ""),
        read_config=lambda _w: "CONFIG_FOO=m\nCONFIG_FAULT=y\n" + _GOOD_TAIL,
        run_make=lambda _w: CapturedStep(0, ""),
    )
    orchestrator.build_workspace(_RUN, _compose_profile(["kdump", "faultinject"]))
    merged = seen[-1].decode()
    assert "CONFIG_FOO=m" in merged and "CONFIG_FOO=y" not in merged


def test_build_workspace_compose_later_disable_is_not_a_dropped_symbol(tmp_path: Path) -> None:
    # A later fragment disabling an earlier =y symbol builds successfully: the net-intent
    # effective fragment emits it as unset, so the drop-check does not flag it.
    fetches = {
        "kdump": b"CONFIG_FOO=y\nCONFIG_SQUASHFS=y\n",
        "faultinject": b"# CONFIG_FOO is not set\n",
    }
    orchestrator = BuildHostOrchestrator.create(
        workspace_root=tmp_path,
        catalog_fetch=lambda name: fetches[name],
        checkout=lambda _r, _p, _w, _f: None,
        run_olddefconfig=lambda _w: CapturedStep(0, ""),
        read_config=lambda _w: "# CONFIG_FOO is not set\n" + _GOOD_TAIL,
        run_make=lambda _w: CapturedStep(0, ""),
    )
    # Does not raise (no spurious "dropped by olddefconfig" for the intentional disable).
    orchestrator.build_workspace(_RUN, _compose_profile(["kdump", "faultinject"]))


def test_build_workspace_single_ref_passes_raw_bytes(tmp_path: Path) -> None:
    # The single-config path must hand the checkout seam the raw fetched bytes unchanged.
    raw = b"# verbatim comment\nCONFIG_SQUASHFS=y\n" + _GOOD_TAIL.encode()
    seen: list[bytes] = []
    orchestrator = BuildHostOrchestrator.create(
        workspace_root=tmp_path,
        catalog_fetch=lambda _n: raw,
        checkout=lambda _r, _p, _w, fragment: seen.append(fragment),
        run_olddefconfig=lambda _w: CapturedStep(0, ""),
        read_config=lambda _w: raw.decode(),
        run_make=lambda _w: CapturedStep(0, ""),
    )
    orchestrator.build_workspace(_RUN, _server_profile())
    assert seen[-1] == raw
