"""Shared native-reader failures retain diagnostic and provenance boundaries."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Never
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve.kdump import extract_dmesg_or_sentinel
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import (
    HostDumpOptions,
    _HostDumpPersister,
)
from kdive.providers.shared.debug_common import core_file
from kdive.security.secrets.secret_registry import SecretRegistry

_MODES = ("shared", "local", "remote")
_NATIVE_ERRORS = (
    "BadDataError",
    "FaultError",
    "MissingDebugInfoError",
    "ObjectAbsentError",
    "OutOfBoundsError",
)
_OPEN_ERRORS = ("OSError", "ValueError", "BadDataError", "FaultError")
_EXTRACTION_ERRORS = ("OSError", "ValueError", "LookupError", *_NATIVE_ERRORS)
_EXPECTED_FAILURES = [
    (stage, error) for stage in ("construction", "opening") for error in _OPEN_ERRORS
] + [("extraction", error) for error in _EXTRACTION_ERRORS]


@dataclass
class _NativeBoundary:
    errors: dict[str, type[Exception]]
    construction_error: BaseException | None = None
    opening_error: BaseException | None = None
    extraction_error: BaseException | None = None
    vmcoreinfo: bytes = b"BUILD-ID=" + b"a" * 40 + b"\n"
    dmesg: bytes = b"kernel log\n"
    opened: list[str] = field(default_factory=list)
    extraction_calls: int = 0


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> _NativeBoundary:
    errors: dict[str, type[Exception]] = {
        name: type(name, (Exception,), {}) for name in _NATIVE_ERRORS
    }
    errors.update(OSError=OSError, ValueError=ValueError, LookupError=LookupError)
    boundary = _NativeBoundary(errors=errors)
    modules = {}
    for name in ("drgn", "drgn.helpers", "drgn.helpers.linux", "drgn.helpers.linux.printk"):
        module = ModuleType(name)
        module.__dict__["__path__"] = []
        monkeypatch.setitem(sys.modules, name, module)
        modules[name] = module

    class Program:
        def __init__(self) -> None:
            if boundary.construction_error is not None:
                raise boundary.construction_error

        def set_core_dump(self, path: str) -> None:
            boundary.opened.append(path)
            if boundary.opening_error is not None:
                raise boundary.opening_error

        def __getitem__(self, name: str) -> SimpleNamespace:
            assert name == "VMCOREINFO"
            return SimpleNamespace(value_=lambda: boundary.vmcoreinfo)

    def get_dmesg(program: Program) -> bytes:
        assert isinstance(program, Program)
        boundary.extraction_calls += 1
        if boundary.extraction_error is not None:
            raise boundary.extraction_error
        return boundary.dmesg

    modules["drgn"].__dict__.update(errors, Program=Program)
    modules["drgn.helpers.linux.printk"].__dict__["get_dmesg"] = get_dmesg
    return boundary


def _no_store() -> Never:
    raise AssertionError("failed provenance or diagnostic fallback must not access storage")


def _persister() -> _HostDumpPersister:
    return _HostDumpPersister(
        secret_registry=SecretRegistry(),
        store_factory=_no_store,
        options=HostDumpOptions(
            core_build_id_from_file=core_file.read_core_build_id_from_file,
            core_dmesg_from_file=core_file.read_core_dmesg_from_file,
            dump_format=0,
            max_core_bytes=core_file.MAX_CORE_BYTES,
        ),
    )


def _reader(mode: str) -> Callable[[Path], bytes]:
    if mode == "shared":
        return core_file.read_core_dmesg_from_file
    if mode == "local":
        return lambda core: extract_dmesg_or_sentinel(core, core_file.read_core_dmesg_from_file)
    assert mode == "remote"
    persister = _persister()
    return lambda core: persister._dmesg_best_effort(core, UUID(int=1))


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("missing", ("drgn", "drgn.helpers.linux.printk"))
def test_missing_reader_dependency_remains_fatal(
    native: _NativeBoundary,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    missing: str,
) -> None:
    monkeypatch.setitem(sys.modules, missing, None)
    with pytest.raises(CategorizedError) as caught:
        _reader(mode)(tmp_path / "core")
    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert isinstance(caught.value.__cause__, ImportError)
    assert native.extraction_calls == 0


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("stage,error", _EXPECTED_FAILURES)
def test_expected_native_failure_uses_existing_diagnostic_policy(
    native: _NativeBoundary, tmp_path: Path, mode: str, stage: str, error: str
) -> None:
    fault = native.errors[error]("native reader failure")
    setattr(native, f"{stage}_error", fault)
    if mode == "shared":
        with pytest.raises(CategorizedError) as caught:
            _reader(mode)(tmp_path / "core")
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert caught.value.__cause__ is fault
        assert caught.value.details == {}
    else:
        assert _reader(mode)(tmp_path / "core") == core_file.DMESG_UNAVAILABLE
    assert native.extraction_calls == (1 if stage == "extraction" else 0)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("stage", ("construction", "opening", "extraction"))
@pytest.mark.parametrize(
    "error", (RuntimeError, TypeError, KeyboardInterrupt, asyncio.CancelledError)
)
def test_unexpected_reader_fault_preserves_existing_owner_policy(
    native: _NativeBoundary,
    tmp_path: Path,
    mode: str,
    stage: str,
    error: type[BaseException],
) -> None:
    fault = error("unexpected reader fault")
    setattr(native, f"{stage}_error", fault)
    if mode == "remote" and isinstance(fault, Exception):
        assert _reader(mode)(tmp_path / "core") == core_file.DMESG_UNAVAILABLE
    else:
        with pytest.raises(error) as caught:
            _reader(mode)(tmp_path / "core")
        assert caught.value is fault


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("data", (b"", b"kernel\xfflog\n"))
def test_reader_preserves_successful_bytes(
    native: _NativeBoundary, tmp_path: Path, mode: str, data: bytes
) -> None:
    native.dmesg = data
    assert _reader(mode)(tmp_path / "core") == data


def test_build_id_reader_preserves_valid_vmcoreinfo(
    native: _NativeBoundary, tmp_path: Path
) -> None:
    assert core_file.read_core_build_id_from_file(tmp_path / "core") == "a" * 40
    assert native.opened == [str(tmp_path / "core")]


def test_missing_build_id_stops_capture_before_diagnostics_or_storage(
    native: _NativeBoundary, tmp_path: Path
) -> None:
    native.vmcoreinfo = b"PAGESIZE=4096\n"
    with pytest.raises(CategorizedError) as caught:
        _persister().capture_output(
            UUID(int=1), UUID(int=2), tmp_path / "core", execution_id=UUID(int=3)
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "BUILD-ID" in str(caught.value)
    assert native.extraction_calls == 0


@pytest.mark.parametrize("stage", ("construction", "opening"))
@pytest.mark.parametrize("error", (OSError, ValueError))
def test_core_open_failure_stops_capture_before_diagnostics_or_storage(
    native: _NativeBoundary, tmp_path: Path, stage: str, error: type[Exception]
) -> None:
    fault = error("unreadable core")
    setattr(native, f"{stage}_error", fault)
    with pytest.raises(CategorizedError) as caught:
        _persister().capture_output(
            UUID(int=1), UUID(int=2), tmp_path / "core", execution_id=UUID(int=3)
        )
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.__cause__ is fault
    assert native.extraction_calls == 0
