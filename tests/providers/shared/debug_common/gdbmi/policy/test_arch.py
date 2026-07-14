"""Unit tests for the cross-arch gdb selection helpers (ADR-0347, #1149).

The three helpers are pure: an ELF-header arch reader, a name-based gdb binary selector with a
multiarch fallback, and a kdive-arch -> gdb-architecture-name map. They carry the coverage for
the change; the engine's ``attach`` that consumes them is ``live_vm``-only.
"""

from __future__ import annotations

import struct
from pathlib import Path

from kdive.providers.shared.debug_common.gdbmi.policy.arch import (
    arch_from_elf,
    gdb_target_arch_name,
    select_gdb_binary,
)

_ELFDATA2LSB = 1
_ELFDATA2MSB = 2
_EM_386 = 3
_EM_PPC64 = 21
_EM_X86_64 = 62


def _elf_header(*, e_machine: int, ei_data: int = _ELFDATA2LSB) -> bytes:
    """A 20-byte ELF prefix: magic, class/data ident, and ``e_machine`` at offset 18."""
    ident = b"\x7fELF" + bytes([2, ei_data, 1]) + b"\x00" * 9  # 16-byte e_ident
    endian = "<" if ei_data == _ELFDATA2LSB else ">"
    e_type = struct.pack(f"{endian}H", 2)  # ET_EXEC
    e_machine_bytes = struct.pack(f"{endian}H", e_machine)
    return ident + e_type + e_machine_bytes


def _write(tmp_path: Path, data: bytes) -> Path:
    target = tmp_path / "vmlinux"
    target.write_bytes(data)
    return target


class TestArchFromElf:
    def test_x86_64_lsb(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _elf_header(e_machine=_EM_X86_64))
        assert arch_from_elf(path) == "x86_64"

    def test_ppc64le_lsb(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _elf_header(e_machine=_EM_PPC64, ei_data=_ELFDATA2LSB))
        assert arch_from_elf(path) == "ppc64le"

    def test_ppc64_big_endian_unsupported(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _elf_header(e_machine=_EM_PPC64, ei_data=_ELFDATA2MSB))
        assert arch_from_elf(path) is None

    def test_unsupported_machine(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _elf_header(e_machine=_EM_386))
        assert arch_from_elf(path) is None

    def test_not_an_elf(self, tmp_path: Path) -> None:
        path = _write(tmp_path, b"MZ\x90\x00" + b"\x00" * 32)
        assert arch_from_elf(path) is None

    def test_truncated(self, tmp_path: Path) -> None:
        path = _write(tmp_path, b"\x7fELF")
        assert arch_from_elf(path) is None

    def test_missing_file(self, tmp_path: Path) -> None:
        assert arch_from_elf(tmp_path / "does-not-exist") is None


class _FakeWhich:
    """A ``shutil.which`` stand-in backed by a name->path dict."""

    def __init__(self, present: dict[str, str]) -> None:
        self._present = present

    def __call__(self, name: str) -> str | None:
        return self._present.get(name)


class TestSelectGdbBinary:
    def test_native_x86(self) -> None:
        which = _FakeWhich({"gdb": "/usr/bin/gdb"})
        assert select_gdb_binary("x86_64", "x86_64", which) == "/usr/bin/gdb"

    def test_native_ppc(self) -> None:
        which = _FakeWhich({"gdb": "/usr/bin/gdb"})
        assert select_gdb_binary("ppc64le", "ppc64le", which) == "/usr/bin/gdb"

    def test_guest_none_is_native(self) -> None:
        which = _FakeWhich({"gdb": "/usr/bin/gdb", "gdb-multiarch": "/usr/bin/gdb-multiarch"})
        assert select_gdb_binary("x86_64", None, which) == "/usr/bin/gdb"

    def test_cross_prefers_multiarch(self) -> None:
        which = _FakeWhich({"gdb": "/usr/bin/gdb", "gdb-multiarch": "/usr/bin/gdb-multiarch"})
        assert select_gdb_binary("x86_64", "ppc64le", which) == "/usr/bin/gdb-multiarch"

    def test_cross_falls_back_to_plain_gdb(self) -> None:
        which = _FakeWhich({"gdb": "/usr/bin/gdb"})
        assert select_gdb_binary("x86_64", "ppc64le", which) == "/usr/bin/gdb"

    def test_cross_none_when_neither_present(self) -> None:
        which = _FakeWhich({})
        assert select_gdb_binary("x86_64", "ppc64le", which) is None

    def test_native_none_when_gdb_absent(self) -> None:
        which = _FakeWhich({"gdb-multiarch": "/usr/bin/gdb-multiarch"})
        assert select_gdb_binary("x86_64", "x86_64", which) is None


class TestGdbTargetArchName:
    def test_known(self) -> None:
        assert gdb_target_arch_name("x86_64") == "i386:x86-64"
        assert gdb_target_arch_name("ppc64le") == "powerpc:common64"

    def test_unknown(self) -> None:
        assert gdb_target_arch_name("s390x") is None
