"""Local-libvirt host-dump capture boundary tests (ADR-0211)."""

from __future__ import annotations

import grp
import os
import stat
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve import host_dump_capture


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


@pytest.mark.parametrize("failure", [None, "dump", "stat", "ceiling", "configuration"])
def test_active_capture_preserves_bytes_cleanup_and_connection(
    spool_root: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:

    paths: list[Path] = []

    class Domain:
        def isActive(self) -> bool:
            return True

        def coreDumpWithFormat(self, name: str, _format: int, _flags: int) -> None:
            core = Path(name)
            paths.append(core)
            assert core.exists()
            if failure == "dump":
                raise host_dump_capture.libvirt.libvirtError("dump failed")
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

    connection = Connection()
    monkeypatch.setattr(host_dump_capture.libvirt, "open", lambda _: connection)
    if failure == "configuration":
        spool_root.rmdir()
    if failure == "ceiling":
        monkeypatch.setattr(host_dump_capture, "MAX_CORE_BYTES", 1)
    try:
        if failure is None:
            core = host_dump_capture._real_host_dump_capture(uuid4())
            assert core is not None and core.read_bytes() == b"vmcore"
        else:
            with pytest.raises(
                FileNotFoundError if failure == "stat" else CategorizedError
            ) as error:
                host_dump_capture._real_host_dump_capture(uuid4())
            if isinstance(error.value, CategorizedError):
                expected = (
                    ErrorCategory.INFRASTRUCTURE_FAILURE
                    if failure == "dump"
                    else ErrorCategory.CONFIGURATION_ERROR
                )
                assert error.value.category == expected
            assert not spool_root.exists() or list(spool_root.iterdir()) == []
        assert connection.closed
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
