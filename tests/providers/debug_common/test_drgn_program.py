"""Tests for the drgn-backed introspection seams (no drgn import off the live host)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.debug_common.drgn_program import (
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
