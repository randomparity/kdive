"""Tests for the ``kdive stage-volume`` orchestration and local config capture (ADR-0336)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.rootfs.stage_volume import (
    StageVolumeDeps,
    _TargetRow,
    capture_kernel_config,
    stage_volume,
)

_ROW_ID = uuid4()
_VOLUME = "fedora-44.qcow2"


class _Recorder:
    """Records the orchestration's calls, in order, and lets each seam be scripted to fail."""

    def __init__(
        self,
        *,
        row: _TargetRow | None = None,
        config: bytes | None = b"CONFIG_X=y\n",
        find_error: CategorizedError | None = None,
        upload_error: CategorizedError | None = None,
        attach_error: CategorizedError | None = None,
    ) -> None:
        self._row = row or _TargetRow(row_id=_ROW_ID, volume=_VOLUME)
        self._config = config
        self._find_error = find_error
        self._upload_error = upload_error
        self._attach_error = attach_error
        self.calls: list[str] = []
        self.uploaded: tuple[str, Path] | None = None
        self.attached: tuple[str, str, str, UUID, bytes] | None = None

    def find_row(self, provider: str, name: str, arch: str) -> _TargetRow:
        self.calls.append("find")
        if self._find_error is not None:
            raise self._find_error
        return self._row

    def capture_config(self, qcow2: Path) -> bytes | None:
        self.calls.append("capture")
        return self._config

    def upload_volume(self, volume: str, qcow2: Path) -> None:
        self.calls.append("upload")
        if self._upload_error is not None:
            raise self._upload_error
        self.uploaded = (volume, qcow2)

    def attach_config(
        self, provider: str, name: str, arch: str, row_id: UUID, config: bytes
    ) -> None:
        self.calls.append("attach")
        if self._attach_error is not None:
            raise self._attach_error
        self.attached = (provider, name, arch, row_id, config)

    def deps(self) -> StageVolumeDeps:
        return StageVolumeDeps(
            find_row=self.find_row,
            capture_config=self.capture_config,
            upload_volume=self.upload_volume,
            attach_config=self.attach_config,
        )


def test_stage_volume_happy_path_uploads_then_attaches(tmp_path: Path) -> None:
    """Row resolved, config captured, volume uploaded, config attached — in order."""
    rec = _Recorder()
    stage_volume("remote-libvirt", "fedora-44", "x86_64", tmp_path / "img.qcow2", rec.deps())
    assert rec.calls == ["find", "capture", "upload", "attach"]
    assert rec.uploaded == (_VOLUME, tmp_path / "img.qcow2")
    assert rec.attached == ("remote-libvirt", "fedora-44", "x86_64", _ROW_ID, b"CONFIG_X=y\n")


def test_stage_volume_missing_row_fails_before_upload(tmp_path: Path) -> None:
    """A missing catalog row fails fast — no volume is uploaded for an unknown image."""
    rec = _Recorder(
        find_error=CategorizedError("no row", category=ErrorCategory.CONFIGURATION_ERROR)
    )
    with pytest.raises(CategorizedError):
        stage_volume("remote-libvirt", "ghost", "x86_64", tmp_path / "img.qcow2", rec.deps())
    assert rec.calls == ["find"]
    assert rec.uploaded is None


def test_stage_volume_upload_failure_is_fatal_and_skips_attach(tmp_path: Path) -> None:
    """A volume-upload fault propagates (fatal) and no config is attached."""
    rec = _Recorder(
        upload_error=CategorizedError("boom", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    )
    with pytest.raises(CategorizedError):
        stage_volume("remote-libvirt", "fedora-44", "x86_64", tmp_path / "img.qcow2", rec.deps())
    assert rec.calls == ["find", "capture", "upload"]
    assert rec.attached is None


def test_stage_volume_no_config_uploads_without_attach(tmp_path: Path) -> None:
    """A None captured config stages the volume with no offer (no attach)."""
    rec = _Recorder(config=None)
    stage_volume("remote-libvirt", "fedora-44", "x86_64", tmp_path / "img.qcow2", rec.deps())
    assert rec.calls == ["find", "capture", "upload"]
    assert rec.uploaded is not None
    assert rec.attached is None


def test_stage_volume_attach_failure_is_advisory(tmp_path: Path) -> None:
    """An attach failure after a successful upload is advisory — the command does not raise."""
    rec = _Recorder(
        attach_error=CategorizedError("s3 down", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    )
    stage_volume("remote-libvirt", "fedora-44", "x86_64", tmp_path / "img.qcow2", rec.deps())
    assert rec.calls == ["find", "capture", "upload", "attach"]
    assert rec.uploaded is not None  # the volume landed despite the attach failure


# --- capture_kernel_config ---------------------------------------------------


def test_capture_kernel_config_single_kernel_reads_config(tmp_path: Path) -> None:
    """Exactly one non-rescue kernel -> the matching /boot/config version is read."""
    seen: list[str] = []

    def _boot(_qcow2: Path) -> list[str]:
        return ["vmlinuz-6.9.0", "config-6.9.0", "initramfs-6.9.0.img"]

    def _config(_qcow2: Path, version: str) -> bytes:
        seen.append(version)
        return b"CONFIG_DEBUG_INFO_BTF=y\n"

    out = capture_kernel_config(
        tmp_path / "img.qcow2", boot_entries_probe=_boot, kernel_config_probe=_config
    )
    assert out == b"CONFIG_DEBUG_INFO_BTF=y\n"
    assert seen == ["6.9.0"]


def test_capture_kernel_config_many_kernels_returns_none(tmp_path: Path) -> None:
    """Two baseline kernels are ambiguous — no version, no config probe."""

    def _boot(_qcow2: Path) -> list[str]:
        return ["vmlinuz-6.9.0", "vmlinuz-6.8.0"]

    def _config(_qcow2: Path, _version: str) -> bytes:  # pragma: no cover - must not be called
        raise AssertionError("config probe must not run for ambiguous /boot")

    assert (
        capture_kernel_config(
            tmp_path / "img.qcow2", boot_entries_probe=_boot, kernel_config_probe=_config
        )
        is None
    )


def test_capture_kernel_config_probe_error_degrades_to_none(tmp_path: Path) -> None:
    """A probe CategorizedError is advisory -> None (never propagates)."""

    def _boot(_qcow2: Path) -> list[str]:
        raise CategorizedError("no guestfish", category=ErrorCategory.MISSING_DEPENDENCY)

    def _config(_qcow2: Path, _version: str) -> bytes:  # pragma: no cover
        raise AssertionError("unreached")

    assert (
        capture_kernel_config(
            tmp_path / "img.qcow2", boot_entries_probe=_boot, kernel_config_probe=_config
        )
        is None
    )


def test_capture_kernel_config_absent_listing_returns_none(tmp_path: Path) -> None:
    """An unproduceable /boot listing (None) degrades to no config."""

    def _boot(_qcow2: Path) -> None:
        return None

    def _config(_qcow2: Path, _version: str) -> bytes:  # pragma: no cover
        raise AssertionError("unreached")

    assert (
        capture_kernel_config(
            tmp_path / "img.qcow2", boot_entries_probe=_boot, kernel_config_probe=_config
        )
        is None
    )
