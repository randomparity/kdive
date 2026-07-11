"""CLI behavior for `build-fs`: the required `--image` catalog path."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.__main__ import build_parser
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.images.rootfs.command import run_build_fs


def _patch_plane(
    monkeypatch: pytest.MonkeyPatch, produced: Path, seen_specs: list[RootfsBuildSpec]
) -> None:
    """Replace the build-plane factory with a fake that records specs and returns ``produced``."""

    class _FakePlane:
        def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
            seen_specs.append(spec)
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    monkeypatch.setattr(
        "kdive.images.rootfs.command._build_local_rootfs_plane",
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
        "kdive.images.rootfs.command._publish_rootfs",
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


def test_build_fs_requires_image() -> None:
    """The legacy no-`--image` virt-builder path is no longer a CLI contract."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["build-fs"])


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


def _patch_plane_provenance(
    monkeypatch: pytest.MonkeyPatch, produced: Path, provenance: dict[str, object]
) -> None:
    """Patch the plane to produce ``produced`` carrying ``provenance``."""

    class _FakePlane:
        def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
            return RootfsBuildOutput(
                qcow2_path=produced, digest="sha256:abc", provenance=provenance
            )

    monkeypatch.setattr(
        "kdive.images.rootfs.command._build_local_rootfs_plane",
        lambda _workspace: _FakePlane(),
    )


def test_build_fs_writes_provenance_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build-fs` records the build's provenance in a sidecar beside the published qcow2 (#977)."""
    import json

    from kdive.images.rootfs.staged_provenance import SIDECAR_SCHEMA, sidecar_path

    produced = tmp_path / "plane" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    provenance: dict[str, object] = {"boot_kernel_count": 1, "makedumpfile_version": "1.7.7"}
    _patch_plane_provenance(monkeypatch, produced, provenance)
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
    doc = json.loads(sidecar_path(dest).read_text(encoding="utf-8"))
    assert doc == {"schema": SIDECAR_SCHEMA, "provenance": provenance}


def test_build_fs_sidecar_write_failure_is_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sidecar-write failure warns and does not fail the build (the qcow2 is the artifact)."""
    import logging

    produced = tmp_path / "plane" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    _patch_plane_provenance(monkeypatch, produced, {"boot_kernel_count": 1})
    dest = tmp_path / "out.qcow2"

    def _boom(_qcow2: Path, *, provenance: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("kdive.images.rootfs.command.write_sidecar", _boom)
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
    with caplog.at_level(logging.WARNING):
        run_build_fs(args)  # does not raise
    assert f"export KDIVE_GUEST_IMAGE={dest}" in capsys.readouterr().out
    assert any("sidecar" in r.getMessage() for r in caplog.records)


def test_build_fs_non_serializable_provenance_is_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-JSON-serializable provenance warns and does not fail the build (advisory)."""
    import logging

    produced = tmp_path / "plane" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    # A set is not JSON-serializable, so write_sidecar's json.dumps raises TypeError.
    _patch_plane_provenance(monkeypatch, produced, {"packages": {"a", "b"}})
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
    with caplog.at_level(logging.WARNING):
        run_build_fs(args)  # does not raise
    assert dest.exists()  # the qcow2 was still published
    assert any("sidecar" in r.getMessage() for r in caplog.records)
