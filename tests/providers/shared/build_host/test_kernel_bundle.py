"""Shared combined kernel+modules bundle seam (ADR-0081, used by both build planes)."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.build_transport import BuildTransport
from kdive.providers.shared.build_host.publishing import kernel_bundle


def _write_fake_build_tree(workspace: Path, mod_root: Path, version: str = "6.9.0") -> None:
    """A minimal workspace + INSTALL_MOD_PATH staging tree for bundle packaging."""
    bzimage = workspace / "arch" / "x86" / "boot" / "bzImage"
    bzimage.parent.mkdir(parents=True)
    bzimage.write_bytes(b"vmlinuz-bytes")
    moddir = mod_root / "lib" / "modules" / version
    (moddir / "kernel" / "drivers").mkdir(parents=True)
    (moddir / "kernel" / "drivers" / "virtio_blk.ko").write_bytes(b"module-bytes")
    (moddir / "modules.dep").write_text("virtio_blk.ko:\n")
    # The back-reference symlinks make modules_install plants (absolute worker paths).
    (moddir / "build").symlink_to(workspace)
    (moddir / "source").symlink_to(workspace)


def test_make_kernel_bundle_bytes_includes_vmlinuz_and_modules_excludes_backrefs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    mod_root = tmp_path / "stage"
    _write_fake_build_tree(workspace, mod_root)

    data = kernel_bundle.make_kernel_bundle_bytes(workspace, mod_root)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "boot/vmlinuz" in names
    assert "lib/modules/6.9.0/kernel/drivers/virtio_blk.ko" in names
    # the dangling absolute back-reference symlinks are stripped
    assert "lib/modules/6.9.0/build" not in names
    assert "lib/modules/6.9.0/source" not in names


def test_make_kernel_bundle_bytes_is_valid_gzip(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    mod_root = tmp_path / "stage"
    _write_fake_build_tree(workspace, mod_root)

    data = kernel_bundle.make_kernel_bundle_bytes(workspace, mod_root)

    assert data[:2] == b"\x1f\x8b"  # gzip magic


def test_make_kernel_bundle_bytes_all_builtin_modules_tree_bundles(tmp_path: Path) -> None:
    # CONFIG_MODULES=n leaves only a modules.builtin/modules.dep tree (no .ko). The bundle must
    # still package without error — local now always runs modules_install, matching remote.
    workspace = tmp_path / "ws"
    mod_root = tmp_path / "stage"
    bzimage = workspace / "arch" / "x86" / "boot" / "bzImage"
    bzimage.parent.mkdir(parents=True)
    bzimage.write_bytes(b"vmlinuz-bytes")
    moddir = mod_root / "lib" / "modules" / "6.9.0"
    moddir.mkdir(parents=True)
    (moddir / "modules.builtin").write_text("kernel/drivers/virtio_blk.ko\n")
    (moddir / "modules.dep").write_text("")

    data = kernel_bundle.make_kernel_bundle_bytes(workspace, mod_root)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "boot/vmlinuz" in names
    assert "lib/modules/6.9.0/modules.builtin" in names


def test_make_kernel_bundle_bytes_missing_bzimage_is_build_failure(tmp_path: Path) -> None:
    # A zero-exit make that left no bzImage must surface as a typed BUILD_FAILURE, not a bare
    # OSError that escapes the provider error contract.
    workspace = tmp_path / "ws"
    mod_root = tmp_path / "stage"
    (mod_root / "lib" / "modules" / "6.9.0").mkdir(parents=True)  # modules exist, bzImage absent

    with pytest.raises(CategorizedError) as caught:
        kernel_bundle.make_kernel_bundle_bytes(workspace, mod_root)

    assert caught.value.category is ErrorCategory.BUILD_FAILURE
    assert str(caught.value) == "kernel bundle could not be packaged"
    assert caught.value.details == {"output": "bzImage"}


def test_make_kernel_bundle_bytes_module_oserror_is_build_failure_module_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A module file that vanishes mid-pack must surface as a typed BUILD_FAILURE whose details
    # name the *module bundle* output (distinct from the bzImage face), not a bare OSError.
    workspace = tmp_path / "ws"
    mod_root = tmp_path / "stage"
    _write_fake_build_tree(workspace, mod_root)

    real_add = tarfile.TarFile.add

    def _add(self: tarfile.TarFile, name: Any, *args: Any, **kwargs: Any) -> None:
        if str(name).endswith(".ko"):
            raise OSError("module file vanished")
        return real_add(self, name, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "add", _add)

    with pytest.raises(CategorizedError) as caught:
        kernel_bundle.make_kernel_bundle_bytes(workspace, mod_root)

    assert caught.value.category is ErrorCategory.BUILD_FAILURE
    assert caught.value.details == {"output": "module bundle"}


def test_build_bundle_member_dirs_keeps_non_backref_symlinks(tmp_path: Path) -> None:
    # Only the absolute back-reference symlinks (``build``/``source``) are dropped; a regular
    # symlink that is *not* a back-ref stays in the member list (guards ``and`` -> ``or``).
    modules_root = tmp_path / "lib" / "modules" / "6.9.0"
    (modules_root / "kernel").mkdir(parents=True)
    real_ko = modules_root / "kernel" / "virtio_blk.ko"
    real_ko.write_bytes(b"module-bytes")
    (modules_root / "build").symlink_to(tmp_path)  # dropped back-ref
    (modules_root / "source").symlink_to(tmp_path)  # dropped back-ref
    (modules_root / "vmlinuz.link").symlink_to(real_ko)  # kept: not a back-ref name

    members = kernel_bundle.build_bundle_member_dirs(modules_root)
    names = {p.name for p in members}

    assert "vmlinuz.link" in names  # non-backref symlink survives
    assert "virtio_blk.ko" in names
    assert "build" not in names
    assert "source" not in names


def test_local_kernel_bundle_returns_bytes_source(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    mod_root = tmp_path / "stage"
    _write_fake_build_tree(workspace, mod_root)

    source = kernel_bundle.local_kernel_bundle(workspace, mod_root)

    assert isinstance(source, kernel_bundle.ArtifactBytes)
    assert source.data[:2] == b"\x1f\x8b"


def test_transport_kernel_bundle_runs_renaming_excluding_tar(tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    class _FakeResult:
        returncode = 0
        stderr = ""

    class _FakeTransport:
        def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> _FakeResult:
            captured["argv"] = argv
            return _FakeResult()

    seam = kernel_bundle.transport_kernel_bundle(cast(BuildTransport, _FakeTransport()))
    source = seam(tmp_path / "ws", tmp_path / "stage")

    argv = captured["argv"]
    assert "--exclude=*/build" in argv
    assert "--exclude=*/source" in argv
    assert any("bzImage" in tok and "boot/vmlinuz" in tok for tok in argv)
    assert isinstance(source, kernel_bundle.ArtifactRemoteFile)


def test_transport_kernel_bundle_nonzero_is_build_failure(tmp_path: Path) -> None:
    class _FakeResult:
        returncode = 2
        stderr = "tar: boom"

    class _FakeTransport:
        def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> _FakeResult:
            return _FakeResult()

    seam = kernel_bundle.transport_kernel_bundle(cast(BuildTransport, _FakeTransport()))

    with pytest.raises(CategorizedError) as caught:
        seam(tmp_path / "ws", tmp_path / "stage")

    assert caught.value.category is ErrorCategory.BUILD_FAILURE
