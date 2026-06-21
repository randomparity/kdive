"""Tests for the local-libvirt kdump host-side overlay harvest (ADR-0203)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve_kdump import (
    GuestCoreReader,
    VmcoreEntry,
    extract_dmesg_or_sentinel,
    file_sha256_b64,
    harvest_vmcore,
    read_via_tempfile,
    redact_dmesg,
    select_newest,
)
from kdive.providers.shared.debug_common.core_file import DMESG_UNAVAILABLE
from kdive.security.secrets.secret_registry import SecretRegistry

_OVERLAY = "/var/lib/kdive/rootfs/sys-overlay.qcow2"


@dataclass
class _FakeReader:
    entries: list[VmcoreEntry]
    blobs: dict[str, bytes] = field(default_factory=dict)
    downloads: list[str] = field(default_factory=list)

    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]:
        return list(self.entries)

    def download_vmcore(self, overlay: str, path: str, dest: Path) -> None:
        self.downloads.append(path)
        dest.write_bytes(self.blobs[path])


def test_select_newest_picks_highest_mtime() -> None:
    entries = [
        VmcoreEntry("/var/crash/a/vmcore", 100.0, 10),
        VmcoreEntry("/var/crash/c/vmcore", 300.0, 30),
        VmcoreEntry("/var/crash/b/vmcore", 200.0, 20),
    ]
    assert select_newest(entries) == entries[1]


def test_select_newest_empty_is_none() -> None:
    assert select_newest([]) is None


def test_harvest_downloads_newest_core_to_dest(tmp_path: Path) -> None:
    reader = _FakeReader(
        entries=[
            VmcoreEntry("/var/crash/old/vmcore", 100.0, 3),
            VmcoreEntry("/var/crash/new/vmcore", 200.0, 5),
        ],
        blobs={"/var/crash/new/vmcore": b"NEWER"},
    )
    dest = tmp_path / "core.vmcore"
    out = harvest_vmcore(reader, _OVERLAY, dest=dest, max_bytes=1024)
    assert out == dest
    assert dest.read_bytes() == b"NEWER"
    assert reader.downloads == ["/var/crash/new/vmcore"]


def test_harvest_absent_core_returns_none(tmp_path: Path) -> None:
    reader = _FakeReader(entries=[])
    dest = tmp_path / "core.vmcore"
    assert harvest_vmcore(reader, _OVERLAY, dest=dest, max_bytes=1024) is None
    assert not dest.exists()


def test_harvest_oversize_core_is_configuration_error(tmp_path: Path) -> None:
    reader = _FakeReader(
        entries=[VmcoreEntry("/var/crash/big/vmcore", 100.0, 4096)],
        blobs={"/var/crash/big/vmcore": b"X"},
    )
    dest = tmp_path / "core.vmcore"
    with pytest.raises(CategorizedError) as exc:
        harvest_vmcore(reader, _OVERLAY, dest=dest, max_bytes=1024)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert reader.downloads == []  # rejected before downloading the bytes
    assert not dest.exists()


def test_guest_core_reader_protocol_is_runtime_checkable() -> None:
    assert isinstance(_FakeReader(entries=[]), GuestCoreReader)


def test_file_sha256_b64_matches_hashlib(tmp_path: Path) -> None:
    core = tmp_path / "core.bin"
    core.write_bytes(b"COREBYTES")
    expected = base64.b64encode(hashlib.sha256(b"COREBYTES").digest()).decode("ascii")
    assert file_sha256_b64(core) == expected


def test_read_via_tempfile_passes_a_path_holding_the_bytes() -> None:
    seen: dict[str, bytes] = {}

    def reader(path: Path) -> str:
        seen["bytes"] = path.read_bytes()
        return "deadbeef"

    assert read_via_tempfile(b"COREBYTES", reader) == "deadbeef"
    assert seen["bytes"] == b"COREBYTES"


def test_read_via_tempfile_removes_the_temp_file() -> None:
    captured: list[Path] = []

    def reader(path: Path) -> str:
        captured.append(path)
        return "x"

    read_via_tempfile(b"X", reader)
    assert not captured[0].exists()


def test_extract_dmesg_success_returns_bytes(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.write_bytes(b"core")
    assert extract_dmesg_or_sentinel(core, lambda _p: b"kernel log") == b"kernel log"


def test_extract_dmesg_degrades_infrastructure_failure_to_sentinel(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.write_bytes(b"core")

    def boom(_p: Path) -> bytes:
        raise CategorizedError("no debuginfo", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    assert extract_dmesg_or_sentinel(core, boom) == DMESG_UNAVAILABLE


def test_extract_dmesg_reraises_missing_dependency(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.write_bytes(b"core")

    def no_drgn(_p: Path) -> bytes:
        raise CategorizedError("drgn missing", category=ErrorCategory.MISSING_DEPENDENCY)

    with pytest.raises(CategorizedError) as exc:
        extract_dmesg_or_sentinel(core, no_drgn)
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_redact_dmesg_scrubs_a_registered_secret(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.write_bytes(b"core")
    registry = SecretRegistry()
    registry.register("hunter2", scope=None)
    out = redact_dmesg(core, lambda _p: b"login password=hunter2 done", registry)
    assert b"hunter2" not in out
    assert b"password=" in out
