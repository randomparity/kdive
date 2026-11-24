"""Local-libvirt host-dump capture boundary tests (ADR-0211)."""

from __future__ import annotations

import grp
import os
import stat
import struct
from pathlib import Path
from typing import NoReturn
from unittest.mock import create_autospec
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve import host_dump_capture, provider
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore
from tests.providers.local_libvirt.fakes import FakeLibvirtConn, libvirt_error


def test_inactive_domain_has_no_live_host_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Domain:
        def isActive(self) -> bool:
            return False

    class _Connection:
        closed = False

        def lookupByName(self, _name: str) -> _Domain:
            return _Domain()

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    monkeypatch.setattr(host_dump_capture.libvirt, "open", lambda _uri: connection)

    assert host_dump_capture._real_host_dump_capture(uuid4()) is None
    assert connection.closed is True


@pytest.fixture
def spool_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:

    root = tmp_path / "host-dump"
    root.mkdir(mode=0o3770)
    root.chmod(0o3770)
    monkeypatch.setattr(host_dump_capture, "_HOST_DUMP_ROOT", root, raising=False)
    monkeypatch.setattr(
        grp, "getgrnam", lambda _: grp.struct_group(("shared", "x", os.getgid(), []))
    )
    real_fstat = os.fstat
    ancestors = set(root.parents) | {root}

    def fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if path in ancestors:
            values = list(info)
            values[4] = 0
            if path != root:
                values[0] = info.st_mode & ~0o022
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", fstat)
    return root


def test_prepare_spool_precreates_worker_owned_core(spool_root: Path) -> None:

    core = host_dump_capture._prepare_host_dump()
    for path, mode in ((core.parent, 0o2710), (core, 0o660)):
        info = path.stat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
            os.geteuid(),
            os.getgid(),
            mode,
        )
    assert core.parent.parent == spool_root
    assert core.read_bytes() == b""


@pytest.mark.parametrize("mode", [0o2770, 0o3777, 0o1770, 0o770])
def test_prepare_spool_rejects_wrong_root_mode(spool_root: Path, mode: int) -> None:

    spool_root.chmod(mode)
    with pytest.raises(CategorizedError) as error:
        host_dump_capture._prepare_host_dump()
    assert error.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert list(spool_root.iterdir()) == []


@pytest.mark.parametrize("attribute", ["system.posix_acl_access", "system.posix_acl_default"])
def test_prepare_spool_rejects_outsider_acl_despite_safe_mode(
    spool_root: Path, attribute: str
) -> None:

    entries = [
        (1, 7, 0xFFFFFFFF),
        (2, 7, os.getuid() + 100000),
        (4, 7, 0xFFFFFFFF),
        (16, 7, 0xFFFFFFFF),
        (32, 0, 0xFFFFFFFF),
    ]
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    os.setxattr(spool_root, attribute, acl)
    assert stat.S_IMODE(spool_root.stat().st_mode) == 0o3770
    with pytest.raises(CategorizedError, match="spool preparation"):
        host_dump_capture._prepare_host_dump()
    assert list(spool_root.iterdir()) == []


@pytest.mark.parametrize(
    "unsafe",
    [
        "missing",
        "symlink",
        "acl_unreadable",
        "foreign_root",
        "foreign_ancestor",
        "ancestor_symlink",
        "writable_ancestor",
    ],
)
def test_prepare_spool_refuses_unsafe_setup(
    spool_root: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:

    if unsafe in {"missing", "symlink"}:
        spool_root.rmdir()
        if unsafe == "symlink":
            spool_root.symlink_to(spool_root.parent, target_is_directory=True)
    elif unsafe == "ancestor_symlink":
        link = spool_root.parent / "linked"
        link.symlink_to(spool_root.parent, target_is_directory=True)
        monkeypatch.setattr(host_dump_capture, "_HOST_DUMP_ROOT", link / "host-dump")
    elif unsafe == "acl_unreadable":

        def unreadable(_fd: int) -> list[str]:
            raise PermissionError("ACL inspection denied")

        monkeypatch.setattr(os, "listxattr", unreadable)
    else:
        real_fstat = os.fstat

        def unsafe_stat(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            values = list(info)
            path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            target = spool_root if unsafe == "foreign_root" else spool_root.parent
            if path == target:
                if unsafe.startswith("foreign"):
                    values[4] = 12345
                else:
                    values[0] = info.st_mode | 0o020
            return os.stat_result(values)

        monkeypatch.setattr(os, "fstat", unsafe_stat)
    with pytest.raises(CategorizedError, match="rerun local host preparation"):
        host_dump_capture._prepare_host_dump()


@pytest.mark.parametrize(
    ("failure", "expected_category"),
    [
        (None, None),
        ("dump", ErrorCategory.INFRASTRUCTURE_FAILURE),
        ("stat", ErrorCategory.INFRASTRUCTURE_FAILURE),
        ("ceiling", ErrorCategory.CONFIGURATION_ERROR),
        ("configuration", ErrorCategory.CONFIGURATION_ERROR),
    ],
)
@pytest.mark.parametrize("close_exception", [None, libvirt.libvirtError, RuntimeError])
def test_active_capture_preserves_bytes_cleanup_and_connection(
    spool_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
    expected_category: ErrorCategory | None,
    close_exception: type[Exception] | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths: list[Path] = []
    system_id = uuid4()
    dump_fault = libvirt_error(libvirt.VIR_ERR_RPC)
    close_fault = close_exception("private close diagnostic") if close_exception else None

    class Domain:
        def isActive(self) -> bool:
            return True

        def coreDumpWithFormat(self, name: str, format_: int, flags: int) -> None:
            assert format_ == libvirt.VIR_DOMAIN_CORE_DUMP_FORMAT_RAW
            assert flags == libvirt.VIR_DUMP_MEMORY_ONLY
            core = Path(name)
            paths.append(core)
            assert core.exists()
            if failure == "dump":
                raise dump_fault
            if failure == "stat":
                core.unlink()
            else:
                core.write_bytes(b"vmcore")

    class Connection:
        closed = False

        def lookupByName(self, _name: str) -> Domain:
            return Domain()

        def close(self) -> None:
            self.closed = True
            if close_fault is not None:
                raise close_fault

    connection = Connection()
    monkeypatch.setattr(libvirt, "open", lambda _: connection)
    if failure == "configuration":
        spool_root.rmdir()
    if failure == "ceiling":
        monkeypatch.setattr(host_dump_capture, "MAX_CORE_BYTES", 1)
    try:
        if expected_category is None:
            core = host_dump_capture._real_host_dump_capture(system_id)
            assert core is not None and core.read_bytes() == b"vmcore"
        else:
            with pytest.raises(CategorizedError) as error:
                host_dump_capture._real_host_dump_capture(system_id)
            assert error.value.category == expected_category
            if failure == "dump":
                assert error.value.__cause__ is dump_fault
                assert str(error.value) == "local host_dump core-dump failed"
            elif failure == "stat":
                assert isinstance(error.value.__cause__, FileNotFoundError)
                assert error.value.__cause__.filename == str(paths[0])
                assert error.value.details["system_id"] == str(system_id)
                assert error.value.details["operation"] == "stat_host_dump"
            assert not spool_root.exists() or list(spool_root.iterdir()) == []
        assert connection.closed
        if close_fault is not None:
            assert str(close_fault) not in caplog.text
            assert any(
                r.levelname == "WARNING" and "host_dump_capture" in r.getMessage()
                for r in caplog.records
            )
    finally:
        for core in paths:
            host_dump_capture._remove_spool(core)


@pytest.mark.parametrize("missing", [False, True])
def test_prepare_spool_requires_configured_group(
    spool_root: Path, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    def group(_name: str) -> grp.struct_group:
        if missing:
            raise KeyError("shared group missing")
        return grp.struct_group(("shared", "x", os.getgid() + 1, []))

    monkeypatch.setattr(grp, "getgrnam", group)
    with pytest.raises(CategorizedError):
        host_dump_capture._prepare_host_dump()
    assert list(spool_root.iterdir()) == []


@pytest.mark.parametrize("target", ["directory", "core"])
@pytest.mark.parametrize("field", ["owner", "group", "mode"])
def test_prepare_spool_rejects_wrong_created_authority(
    spool_root: Path, monkeypatch: pytest.MonkeyPatch, target: str, field: str
) -> None:
    real_fstat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        matches = path.parent == spool_root if target == "directory" else path.name == "vmcore"
        if matches:
            values = list(info)
            index = {"owner": 4, "group": 5, "mode": 0}[field]
            values[index] = int(values[index]) ^ 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(CategorizedError):
        host_dump_capture._prepare_host_dump()
    assert list(spool_root.iterdir()) == []


def test_prepare_spool_exclusively_creates_core(
    spool_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    observed: list[bytes] = []

    def open_existing(path, flags, mode=0o777, *, dir_fd=None):
        if path == "vmcore" and flags & os.O_CREAT:
            fd = real_open(path, os.O_WRONLY | os.O_CREAT, mode, dir_fd=dir_fd)
            os.write(fd, b"existing")
            os.close(fd)
            try:
                return real_open(path, flags, mode, dir_fd=dir_fd)
            except FileExistsError:
                observed.append((Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path).read_bytes())
                raise
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", open_existing)
    with pytest.raises(CategorizedError):
        host_dump_capture._prepare_host_dump()
    assert observed == [b"existing"]
    assert list(spool_root.iterdir()) == []


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("failure_phase", ["dump", "close"])
def test_host_dump_close_control_cleans_undelivered_core(
    signal: type[BaseException],
    failure_phase: str,
    spool_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = signal("capture interrupted")
    paths: list[Path] = []

    class Domain:
        def isActive(self) -> bool:
            return True

        def coreDumpWithFormat(self, name: str, _format: int, _flags: int) -> None:
            core = Path(name)
            paths.append(core)
            core.write_bytes(b"vmcore")
            if failure_phase == "dump":
                raise control

    class Connection:
        def lookupByName(self, _name: str) -> Domain:
            return Domain()

        def close(self) -> NoReturn:
            if failure_phase == "close":
                raise control
            raise RuntimeError("private close diagnostic")

    monkeypatch.setattr(libvirt, "open", lambda _uri: Connection())
    try:
        with pytest.raises(signal) as caught:
            host_dump_capture._real_host_dump_capture(uuid4())
        assert caught.value is control
        assert list(spool_root.iterdir()) == []
        assert "private close diagnostic" not in caplog.text
    finally:
        for core in paths:
            host_dump_capture._remove_spool(core)


@pytest.mark.parametrize("code", [libvirt.VIR_ERR_NO_DOMAIN, libvirt.VIR_ERR_RPC, None])
def test_host_dump_provider_preserves_native_failure(
    code: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fault = libvirt_error(code if code is not None else libvirt.VIR_ERR_INTERNAL_ERROR)
    if code is None:
        fault.err = None
    connection = FakeLibvirtConn()

    def lookup(_name: str) -> NoReturn:
        raise fault

    monkeypatch.setattr(connection, "lookupByName", lookup)
    monkeypatch.setattr(libvirt, "open", lambda _uri: connection)
    store = create_autospec(ObjectStore, instance=True)
    retrieve = provider.LocalLibvirtRetrieve.from_env(secret_registry=SecretRegistry(), store=store)
    with pytest.raises(CategorizedError) as caught:
        retrieve.capture(uuid4(), uuid4(), CaptureMethod.HOST_DUMP, execution_id=uuid4())
    if code == libvirt.VIR_ERR_NO_DOMAIN:
        assert caught.value.category is ErrorCategory.READINESS_FAILURE
    else:
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert caught.value.__cause__ is fault
    assert store.mock_calls == []
