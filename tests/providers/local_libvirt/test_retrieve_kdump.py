"""Tests for the local-libvirt kdump host-side overlay harvest (ADR-0203)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve_kdump import (
    GuestCoreReader,
    VmcoreEntry,
    harvest_vmcore,
    select_newest,
)

_OVERLAY = "/var/lib/kdive/rootfs/sys-overlay.qcow2"


@dataclass
class _FakeReader:
    entries: list[VmcoreEntry]
    blobs: dict[str, bytes] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)

    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]:
        return list(self.entries)

    def read_vmcore(self, overlay: str, path: str) -> bytes:
        self.reads.append(path)
        return self.blobs[path]


def test_select_newest_picks_highest_mtime() -> None:
    entries = [
        VmcoreEntry("/var/crash/a/vmcore", 100.0, 10),
        VmcoreEntry("/var/crash/c/vmcore", 300.0, 30),
        VmcoreEntry("/var/crash/b/vmcore", 200.0, 20),
    ]
    assert select_newest(entries) == entries[1]


def test_select_newest_empty_is_none() -> None:
    assert select_newest([]) is None


def test_harvest_reads_newest_core_bytes() -> None:
    reader = _FakeReader(
        entries=[
            VmcoreEntry("/var/crash/old/vmcore", 100.0, 3),
            VmcoreEntry("/var/crash/new/vmcore", 200.0, 5),
        ],
        blobs={"/var/crash/new/vmcore": b"NEWER"},
    )
    out = harvest_vmcore(reader, _OVERLAY, max_bytes=1024)
    assert out == b"NEWER"
    assert reader.reads == ["/var/crash/new/vmcore"]


def test_harvest_absent_core_returns_none() -> None:
    reader = _FakeReader(entries=[])
    assert harvest_vmcore(reader, _OVERLAY, max_bytes=1024) is None


def test_harvest_oversize_core_is_configuration_error() -> None:
    reader = _FakeReader(
        entries=[VmcoreEntry("/var/crash/big/vmcore", 100.0, 4096)],
        blobs={"/var/crash/big/vmcore": b"X"},
    )
    with pytest.raises(CategorizedError) as exc:
        harvest_vmcore(reader, _OVERLAY, max_bytes=1024)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert reader.reads == []  # rejected before reading the bytes


def test_guest_core_reader_protocol_is_runtime_checkable() -> None:
    assert isinstance(_FakeReader(entries=[]), GuestCoreReader)
