"""Cross-arch wiring on ``GdbMiEngine`` (ADR-0347, #1149).

``attach`` itself is ``live_vm``-only, but the injectable ``host_arch_finder`` kwarg and the
``_missing_gdb_error`` mapping are plain code and unit-tested here.
"""

from __future__ import annotations

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
