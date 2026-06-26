"""CLI behavior for `build-fs`: the `--image` catalog path and the back-compat default."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.__main__ import build_parser
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.images.rootfs_command import run_build_fs


def _patch_plane(
    monkeypatch: pytest.MonkeyPatch, produced: Path, seen_specs: list[RootfsBuildSpec]
) -> None:
    """Replace the build-plane factory with a fake that records specs and returns ``produced``."""

    class _FakePlane:
        def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
            seen_specs.append(spec)
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    monkeypatch.setattr(
        "kdive.images.rootfs_command._build_local_rootfs_plane",
        lambda _workspace: _FakePlane(),
    )


def test_build_fs_image_resolves_catalog_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build-fs --image <name>` builds the named catalog entry's name/releasever/source."""
    produced = tmp_path / "plane" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    seen_specs: list[RootfsBuildSpec] = []
    _patch_plane(monkeypatch, produced, seen_specs)
    dest = tmp_path / "out.qcow2"
    args = build_parser().parse_args(
        [
            "build-fs",
            "--image",
            "fedora-kdive-ready-44",
            "--workspace",
            str(tmp_path / "ws"),
            "--dest",
            str(dest),
        ]
    )
    run_build_fs(args)
    spec = seen_specs[0]
    assert spec.name == "fedora-kdive-ready-44"
    assert spec.releasever == "44"
    assert spec.distro == "fedora"
    digest = spec.source_image_digest
    assert digest.startswith("cloud-image:") and "sha256:" in digest


def test_build_fs_image_derives_local_rootfs_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `--dest`, `--image` publishes to /var/lib/kdive/rootfs/local/<name>.qcow2."""
    produced = tmp_path / "plane" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    _patch_plane(monkeypatch, produced, [])
    seen_dest: list[Path] = []
    monkeypatch.setattr(
        "kdive.images.rootfs_command._publish_rootfs",
        lambda _output, dest: seen_dest.append(dest),
    )
    args = build_parser().parse_args(
        ["build-fs", "--image", "fedora-kdive-ready-44", "--workspace", str(tmp_path / "ws")]
    )
    run_build_fs(args)
    assert seen_dest == [Path("/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2")]


def test_build_fs_unknown_image_is_configuration_error(tmp_path: Path) -> None:
    """An unknown `--image` surfaces a CONFIGURATION_ERROR, not a traceback."""
    args = build_parser().parse_args(
        ["build-fs", "--image", "no-such-image", "--workspace", str(tmp_path / "ws")]
    )
    with pytest.raises(CategorizedError) as caught:
        run_build_fs(args)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_build_fs_default_path_synthesizes_virt_builder_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-`--image` path keeps the legacy `virt-builder:<distro>-<releasever>` provenance."""
    produced = tmp_path / "plane" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    seen_specs: list[RootfsBuildSpec] = []
    _patch_plane(monkeypatch, produced, seen_specs)
    args = build_parser().parse_args(
        ["build-fs", "--workspace", str(tmp_path / "ws"), "--dest", str(tmp_path / "out.qcow2")]
    )
    run_build_fs(args)
    spec = seen_specs[0]
    assert spec.source_image_digest == "virt-builder:fedora-43"


def test_build_fs_image_resolves_el9_package_set_without_standalone_makedumpfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EL9 `--image` resolves the EL-aware set: kexec-tools, no standalone makedumpfile."""
    produced = tmp_path / "plane" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    seen_specs: list[RootfsBuildSpec] = []
    _patch_plane(monkeypatch, produced, seen_specs)
    args = build_parser().parse_args(
        [
            "build-fs",
            "--image",
            "rocky-kdive-ready-9",
            "--workspace",
            str(tmp_path / "ws"),
            "--dest",
            str(tmp_path / "out.qcow2"),
        ]
    )
    run_build_fs(args)
    spec = seen_specs[0]
    assert spec.distro == "rocky" and spec.releasever == "9"
    assert "kexec-tools" in spec.packages and "drgn" in spec.packages
    assert "makedumpfile" not in spec.packages and "kdump-utils" not in spec.packages
