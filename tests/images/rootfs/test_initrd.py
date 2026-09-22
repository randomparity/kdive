"""Tests for bounded initrd archive rewrites."""

from __future__ import annotations

from compression import zstd
from pathlib import Path

import pytest

from kdive.images.rootfs.initrd import remove_zstd_newc_entry


def _pad(payload: bytes) -> bytes:
    return payload + b"\0" * (-len(payload) % 4)


def _entry(name: str, payload: bytes = b"") -> bytes:
    encoded_name = name.encode() + b"\0"
    fields = (1, 0o100644, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(encoded_name), 0)
    header = b"070701" + b"".join(f"{field:08x}".encode() for field in fields)
    return _pad(header + encoded_name) + _pad(payload)


def _archive(*entries: bytes) -> bytes:
    return b"".join((*entries, _entry("TRAILER!!!"))) + b"\0" * 128


def test_remove_zstd_newc_entry_preserves_other_records(tmp_path: Path) -> None:
    target = "var/lib/dracut/hooks/pre-mount/20-kiwi-repart-disk.sh"
    path = tmp_path / "initrd"
    path.write_bytes(zstd.compress(_archive(_entry(target, b"bad"), _entry("etc/keep", b"ok"))))

    assert remove_zstd_newc_entry(path, target)

    unpacked = zstd.decompress(path.read_bytes())
    assert target.encode() + b"\0" not in unpacked
    assert b"etc/keep\0" in unpacked
    assert b"ok" in unpacked
    assert b"TRAILER!!!\0" in unpacked


def test_remove_zstd_newc_entry_is_noop_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "initrd"
    original = zstd.compress(_archive(_entry("etc/keep", b"ok")))
    path.write_bytes(original)

    assert not remove_zstd_newc_entry(path, "missing")
    assert path.read_bytes() == original


def test_remove_zstd_newc_entry_rejects_malformed_archive(tmp_path: Path) -> None:
    path = tmp_path / "initrd"
    original = zstd.compress(b"not-cpio")
    path.write_bytes(original)

    with pytest.raises(ValueError, match="newc header"):
        remove_zstd_newc_entry(path, "missing")
    assert path.read_bytes() == original
