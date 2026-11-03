"""Directory authority for shared provider cleanup (ADR-0739)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle import storage


@pytest.mark.parametrize("mode", [0o2700, 0o2755, 0o3775])
def test_adds_only_group_access_and_preserves_contents(tmp_path: Path, mode: int) -> None:
    directory = tmp_path / "artifact"
    directory.mkdir()
    directory.chmod(mode)
    artifact = directory / "kernel"
    artifact.write_bytes(b"kernel")
    artifact.chmod(0o600)
    before = directory.stat()

    storage.ensure_shared_artifact_directory(directory)

    after = directory.stat()
    assert stat.S_IMODE(after.st_mode) == mode | 0o070
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert artifact.read_bytes() == b"kernel"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


@pytest.mark.parametrize("shared", [False, True])
def test_foreign_owner_requires_preexisting_group_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    tmp_path.chmod(0o770 if shared else 0o750)
    monkeypatch.setattr(storage.os, "geteuid", lambda: tmp_path.stat().st_uid + 1)
    before = tmp_path.stat()
    if shared:
        storage.ensure_shared_artifact_directory(tmp_path)
    else:
        with pytest.raises(CategorizedError, match="owner or administrator") as caught:
            storage.ensure_shared_artifact_directory(tmp_path)
        assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert caught.value.details["path"] == str(tmp_path)
    assert tmp_path.stat() == before


@pytest.mark.parametrize("symlink", [False, True])
def test_rejects_non_directory_leaf_without_mutating_target(tmp_path: Path, symlink: bool) -> None:
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o700)
    leaf = tmp_path / "leaf"
    if symlink:
        leaf.symlink_to(target, target_is_directory=True)
    else:
        leaf.write_bytes(b"sentinel")
    with pytest.raises(OSError):
        storage.ensure_shared_artifact_directory(leaf)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    if not symlink:
        assert leaf.read_bytes() == b"sentinel"


def test_closes_descriptor_when_permission_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    observed: list[int] = []

    def fail(fd: int, _mode: int) -> None:
        observed.append(fd)
        raise PermissionError("denied")

    monkeypatch.setattr(storage.os, "fchmod", fail)
    with pytest.raises(PermissionError, match="denied"):
        storage.ensure_shared_artifact_directory(tmp_path)
    assert len(observed) == 1
    with pytest.raises(OSError):
        os.fstat(observed[0])


def test_unreadable_directory_reports_configuration_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(_path: Path, _flags: int) -> int:
        raise PermissionError("foreign directory denies read")

    monkeypatch.setattr(storage.os, "open", denied)
    with pytest.raises(CategorizedError, match="owner or administrator") as caught:
        storage.ensure_shared_artifact_directory(tmp_path)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert isinstance(caught.value.__cause__, PermissionError)
