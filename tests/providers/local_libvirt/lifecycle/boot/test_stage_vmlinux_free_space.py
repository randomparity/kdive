"""The fixed-worker `_stage_vmlinux` checks guest free space before the upload (#3125)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.boot.guest_free_space import (
    VMLINUX_HEADROOM_BYTES,
)
from kdive.providers.local_libvirt.lifecycle.boot.guest_kernel_writer import (
    _GuestFS,
    _RealGuestKernelWriter,
)

_SIZE = 8192


class _Guest:
    def __init__(self, stats: dict[str, int]) -> None:
        self.stats = stats
        self.calls: list[str] = []
        self.existing = 0

    def mkdir_p(self, path: str) -> None:
        self.calls.append("mkdir_p")

    def statvfs(self, path: str) -> dict[str, int]:
        self.calls.append("statvfs")
        return self.stats

    def upload(self, filename: str, remotefilename: str) -> None:
        self.calls.append("upload")

    def is_file(self, path: str) -> int:
        return int(self.existing > 0)

    def statns(self, path: str) -> dict[str, int]:
        return {"st_size": self.existing or _SIZE}


def _stage(guest: _Guest, tmp_path: Path) -> None:
    vmlinux = tmp_path / "vmlinux"
    vmlinux.write_bytes(b"x" * _SIZE)
    _RealGuestKernelWriter._stage_vmlinux(cast(_GuestFS, guest), "overlay", str(vmlinux), "7.0.0")


def _free(free: int) -> dict[str, int]:
    return {"bavail": free // 4096, "blocks": 1 << 30, "frsize": 4096}


def test_enough_space_uploads_after_the_check(tmp_path: Path) -> None:
    guest = _Guest(_free(VMLINUX_HEADROOM_BYTES + _SIZE))

    _stage(guest, tmp_path)

    assert guest.calls == ["mkdir_p", "statvfs", "upload"]


def test_one_block_short_refuses_without_uploading(tmp_path: Path) -> None:
    guest = _Guest(_free(VMLINUX_HEADROOM_BYTES))

    with pytest.raises(CategorizedError) as caught:
        _stage(guest, tmp_path)

    assert caught.value.category is ErrorCategory.INSTALL_FAILURE
    assert "upload" not in guest.calls


def test_negative_statvfs_values_refuse_without_uploading(tmp_path: Path) -> None:
    guest = _Guest({"bavail": -5, "blocks": -1, "frsize": 4096})

    with pytest.raises(CategorizedError, match="cannot be trusted"):
        _stage(guest, tmp_path)

    assert "upload" not in guest.calls


def test_an_overwritten_vmlinux_counts_as_free_space(tmp_path: Path) -> None:
    guest = _Guest(_free(VMLINUX_HEADROOM_BYTES))
    guest.existing = _SIZE

    _stage(guest, tmp_path)

    assert guest.calls[-1] == "upload"
