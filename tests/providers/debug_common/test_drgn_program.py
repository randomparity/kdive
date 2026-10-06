"""Tests for the drgn-backed introspection seams (no drgn import off the live host)."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.debug_common.drgn_program import (
    _page_split_reader,
    _route_ppc64_vmalloc,
    read_vmcoreinfo_build_id,
    run_introspection_helper,
)

_BUILD_ID = "ab" * 20


def test_read_vmcoreinfo_build_id_parses_the_note_line() -> None:
    vmcoreinfo = b"VMCOREINFO\x00OSRELEASE=7.0.0\nBUILD-ID=%s\nPAGESIZE=4096\n" % _BUILD_ID.encode()
    blob = b"\x00" * 128 + vmcoreinfo
    assert read_vmcoreinfo_build_id(blob) == _BUILD_ID


def test_read_vmcoreinfo_build_id_missing_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        read_vmcoreinfo_build_id(b"no notes here")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == (
        "vmcore carries no VMCOREINFO BUILD-ID line; cannot verify provenance"
    )


def test_read_vmcoreinfo_build_id_rejects_short_hex() -> None:
    with pytest.raises(CategorizedError):
        read_vmcoreinfo_build_id(b"BUILD-ID=abcd\n")


class _FakeProgram:
    def __init__(self, arch: str = "x86_64") -> None:
        # `arch` feeds uts.machine; the "x86_64" default keeps the no-arg callers unchanged.
        self._arch = arch

    def iter_tasks(self) -> list[object]:
        return []

    def iter_modules(self) -> list[object]:
        return []

    def uts(self) -> dict[str, str]:
        return {"release": "7.0.0", "version": "#1", "machine": self._arch, "nodename": "g"}

    def boot_cmdline(self) -> str:
        return "console=ttyS0 root=/dev/vda"

    def cpus_online(self) -> int:
        return 2

    def mem_total_pages(self) -> int:
        return 524288


def test_run_introspection_helper_dispatches_fixed_names() -> None:
    prog = _FakeProgram()
    assert run_introspection_helper(prog, "tasks") == {"tasks": [], "truncated": False}
    assert run_introspection_helper(prog, "modules")["modules"] == []
    sysinfo = run_introspection_helper(prog, "sysinfo")
    assert sysinfo["release"] == "7.0.0"
    assert sysinfo["boot_cmdline"] == "console=ttyS0 root=/dev/vda"


@pytest.mark.parametrize("arch", ["x86_64", "ppc64le"])
def test_run_introspection_helper_sysinfo_reports_guest_arch(arch: str) -> None:
    """The sysinfo helper reports the guest arch verbatim through the fixed-name dispatch (#1150).

    Proves the shared drgn seam is arch-blind: `machine` round-trips whatever the program's uts
    reports, and the tasks/modules dispatch is unaffected by arch.
    """
    prog = _FakeProgram(arch=arch)
    assert run_introspection_helper(prog, "sysinfo")["machine"] == arch
    # Dispatch of the other fixed names is arch-invariant.
    assert run_introspection_helper(prog, "tasks") == {"tasks": [], "truncated": False}
    assert run_introspection_helper(prog, "modules")["modules"] == []


def test_run_introspection_helper_rejects_unknown_name() -> None:
    with pytest.raises(CategorizedError) as exc:
        run_introspection_helper(_FakeProgram(), "files")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "unknown introspection helper: files"


# --- ppc64 vmalloc routing around libkdumpfile's pre-4.x layout (#2763) -----------------------

_PAGE = 0x10000
_VMALLOC_START = 0xC008000000000000


def _fake_physical_memory() -> tuple[Callable[[int], int], Callable[[int, int], bytes], list[int]]:
    """Map virtual page N of the window to physical page 7 - N; memory byte = low byte of phys."""
    translated: list[int] = []

    def translate(address: int) -> int:
        translated.append(address)
        page, offset = divmod(address - _VMALLOC_START, _PAGE)
        return (7 - page) * _PAGE + offset

    def read_physical(address: int, count: int) -> bytes:
        return bytes((address + i) & 0xFF for i in range(count))

    return translate, read_physical, translated


def test_page_split_reader_translates_each_page_of_a_cross_page_read() -> None:
    translate, read_physical, translated = _fake_physical_memory()
    read = _page_split_reader(translate, read_physical, _PAGE)
    address = _VMALLOC_START + _PAGE - 3
    data = read(address, 6, 0, False)
    expected_phys = [7 * _PAGE - 3 + i for i in range(3)] + [6 * _PAGE + i for i in range(3)]
    assert data == bytes(p & 0xFF for p in expected_phys)
    assert translated == [address, _VMALLOC_START + _PAGE]


def test_page_split_reader_reads_inside_one_page_with_one_translation() -> None:
    translate, read_physical, translated = _fake_physical_memory()
    read = _page_split_reader(translate, read_physical, _PAGE)
    assert len(read(_VMALLOC_START + 0x10, 0x20, 0, False)) == 0x20
    assert translated == [_VMALLOC_START + 0x10]


def test_page_split_reader_propagates_a_translation_fault() -> None:
    def translate(address: int) -> int:
        raise LookupError(f"unmapped {address:#x}")

    read = _page_split_reader(translate, lambda address, count: b"", _PAGE)
    with pytest.raises(LookupError, match="unmapped"):
        read(_VMALLOC_START, 8, 0, False)


class _FakeObject:
    def __init__(self, value: int) -> None:
        self._value = value

    def value_(self) -> int:
        return self._value

    def address_of_(self) -> _FakeObject:
        return self


class _FakeDrgnProgram:
    def __init__(self, arch: str, symbols: dict[str, int]) -> None:
        self.platform = SimpleNamespace(arch=arch)
        self._symbols = symbols
        self.segments: list[tuple[int, int, Any]] = []

    def __getitem__(self, name: str) -> _FakeObject:
        return _FakeObject(self._symbols[name])

    def add_memory_segment(self, address: int, size: int, read_fn: Any) -> None:
        self.segments.append((address, size, read_fn))


_FAKE_DRGN = SimpleNamespace(Architecture=SimpleNamespace(PPC64="PPC64", X86_64="X86_64"))
_BOOK3S_SYMBOLS = {
    "__vmalloc_start": _VMALLOC_START,
    "init_mm": 0xC000000002A00000,
    "PAGE_SIZE": _PAGE,
}


def test_route_ppc64_vmalloc_overrides_the_non_linear_tail_of_the_0xc_region() -> None:
    prog = _FakeDrgnProgram("PPC64", _BOOK3S_SYMBOLS)
    _route_ppc64_vmalloc(_FAKE_DRGN, prog, lambda mm, address: address)
    [(address, size, read_fn)] = prog.segments
    assert (address, address + size) == (_VMALLOC_START, 0xD000000000000000)
    assert callable(read_fn)


def test_route_ppc64_vmalloc_leaves_other_arches_to_the_core_reader() -> None:
    prog = _FakeDrgnProgram("X86_64", _BOOK3S_SYMBOLS)
    _route_ppc64_vmalloc(_FAKE_DRGN, prog, lambda mm, address: address)
    assert prog.segments == []


def test_route_ppc64_vmalloc_skips_a_kernel_without_the_book3s_layout_symbol() -> None:
    prog = _FakeDrgnProgram("PPC64", {"init_mm": 0xC000000002A00000, "PAGE_SIZE": _PAGE})
    _route_ppc64_vmalloc(_FAKE_DRGN, prog, lambda mm, address: address)
    assert prog.segments == []
