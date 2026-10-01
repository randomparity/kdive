"""Cross-arch wiring on ``GdbMiEngine`` (ADR-0347, #1149).

``attach`` itself is ``live_vm``-only, but the injectable ``host_arch_finder`` kwarg and the
``_missing_gdb_error`` mapping are plain code and unit-tested here.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.providers.shared.debug_common.gdbmi.core.engine import GdbMiEngine


def test_engine_accepts_host_arch_finder() -> None:
    engine = GdbMiEngine(host_arch_finder=lambda: "x86_64", gdb_path_finder=lambda _name: None)
    assert engine._host_arch_finder() == "x86_64"


def test_missing_gdb_error_native() -> None:
    error = GdbMiEngine._missing_gdb_error(is_cross_arch=False, guest_arch=None)
    assert error.category is ErrorCategory.MISSING_DEPENDENCY
    assert error.details["missing_tools"] == ["gdb"]
    assert "gdb-multiarch" not in str(error)


def test_missing_gdb_error_cross_arch_names_multiarch() -> None:
    error = GdbMiEngine._missing_gdb_error(is_cross_arch=True, guest_arch="ppc64le")
    assert error.category is ErrorCategory.MISSING_DEPENDENCY
    assert error.details["missing_tools"] == ["gdb-multiarch", "gdb"]
    assert error.details["guest_arch"] == "ppc64le"
    assert "gdb-multiarch" in str(error)
    assert "ppc64le" in str(error)


_EM_PPC64 = 21
_EM_X86_64 = 62


class _DoneController:
    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        return [{"type": "result", "message": "done", "payload": None}]

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        return []

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        return []

    def exit(self) -> None:
        return None


def _elf(tmp_path: Path, e_machine: int) -> Path:
    ident = b"\x7fELF" + bytes([2, 1, 1]) + b"\x00" * 9
    path = tmp_path / "vmlinux"
    path.write_bytes(ident + struct.pack("<HH", 2, e_machine))
    return path


@pytest.mark.parametrize(
    ("e_machine", "arch"), [(_EM_X86_64, "x86_64"), (_EM_PPC64, "ppc64le"), (3, None)]
)
def test_attach_records_guest_arch(tmp_path: Path, e_machine: int, arch: str | None) -> None:
    engine = GdbMiEngine(
        controller_factory=lambda _command: _DoneController(),
        gdb_path_finder=lambda _name: "/usr/bin/gdb",
        host_arch_finder=lambda: "x86_64",
        sleep=lambda _sec: None,
    )
    attachment = engine.attach(
        host="127.0.0.1",
        port=1234,
        vmlinux_path=_elf(tmp_path, e_machine),
        transcript_path=tmp_path / "transcript.log",
    )
    assert attachment.guest_arch == arch
