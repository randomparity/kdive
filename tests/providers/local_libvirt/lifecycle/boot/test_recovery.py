"""Contract tests for bounded local-libvirt module recovery (ADR-0586)."""

from __future__ import annotations

import base64
import hashlib
import inspect
import io
import os
import stat
import tarfile
import unicodedata
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from kdive.providers.local_libvirt.lifecycle.boot import recovery
from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    AbsentModuleCapture,
    ModuleArchiveCapture,
    RealGuestRecoveryWriter,
    RecoveryArchiveSink,
    RecoveryArchiveSource,
)
from kdive.providers.ports.external_boot import AbsentComponentState


def _entry(**changes: object) -> recovery._Entry:
    values: dict[str, object] = {
        "path": "kernel.ko",
        "kind": "regular",
        "mode": "0644",
        "uid": 0,
        "gid": 0,
        "size": 3,
        "sha256": "sha256:" + hashlib.sha256(b"abc").hexdigest(),
        "target": None,
        "xattrs_supported": True,
        "xattrs": {},
    }
    values.update(changes)
    return recovery._Entry.model_validate(values)


def _capture(data: bytes, entries: list[recovery._Entry]) -> ModuleArchiveCapture:
    _, manifest, count, size = recovery._manifest(entries)
    return ModuleArchiveCapture(
        manifest=manifest,
        entry_count=count,
        uncompressed_bytes=size,
        archive_sha256="sha256:" + hashlib.sha256(data).hexdigest(),
    )


def _directory_fd(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def test_empty_manifest_has_frozen_canonical_bytes_and_digest() -> None:
    payload, digest, count, size = recovery._manifest([])

    assert payload == b'{"entries":[],"schema":"recovery-module-tree-v1"}'
    assert digest == "sha256:7048c9e065ecf77a964188f42aaebb79a3e8238ecc47736ae47239b8ceec30a5"
    assert (count, size) == (0, 0)


def test_manifest_orders_utf8_paths_and_freezes_regular_metadata() -> None:
    first = _entry(path="zeta", xattrs_supported=False)
    second = _entry(
        path="álpha",
        xattrs={
            "security.selinux": base64.b64encode(b"label\0").decode().rstrip("="),
            "system.posix_acl_access": base64.b64encode(b"acl").decode().rstrip("="),
        },
    )

    payload, _, _, _ = recovery._manifest([second, first])
    decoded = recovery.json.loads(payload)

    assert [item["path"] for item in decoded["entries"]] == ["zeta", "álpha"]
    assert decoded["entries"][0] == first.model_dump(mode="json")
    assert decoded["entries"][1]["xattrs"] == second.xattrs
    assert payload.endswith(b"}") and not payload.endswith(b"\n")


def test_archive_round_trip_preserves_absolute_symlink_and_xattrs() -> None:
    entries = [
        _entry(path="sub", kind="directory", mode="0750", size=0, sha256=None),
        _entry(path="sub/kernel.ko"),
        _entry(
            path="build",
            kind="symlink",
            mode="0777",
            size=0,
            sha256=None,
            target="/usr/src/kernels/current",
            xattrs={"security.selinux": "YWJj"},
        ),
    ]

    archive = recovery._archive(entries, {"sub/kernel.ko": b"abc"})
    observed, contents = recovery._read_archive(archive)

    assert observed == sorted(entries, key=lambda entry: entry.path.encode())
    assert contents == {"sub/kernel.ko": b"abc"}


@pytest.mark.parametrize("path", ["/absolute", "../escape", "a/../escape", "a//b", "./a"])
def test_archive_rejects_noncanonical_paths(path: str) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo(path)
        member.pax_headers = {"KDIVE.xattrs-supported": "1"}
        archive.addfile(member)

    with pytest.raises(ValueError, match="path is not canonical"):
        recovery._read_archive(buffer.getvalue())


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_archive_rejects_special_files(kind: bytes) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("hostile")
        member.type = kind
        member.pax_headers = {"KDIVE.xattrs-supported": "1"}
        archive.addfile(member)

    with pytest.raises(ValueError, match="forbidden topology"):
        recovery._read_archive(buffer.getvalue())


def test_archive_rejects_hard_links_and_duplicates() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("linked")
        member.type = tarfile.LNKTYPE
        member.linkname = "other"
        member.pax_headers = {"KDIVE.xattrs-supported": "1"}
        archive.addfile(member)
    with pytest.raises(ValueError, match="forbidden topology"):
        recovery._read_archive(buffer.getvalue())

    with pytest.raises(ValueError, match="duplicate"):
        recovery._manifest([_entry(), _entry()])


def test_manifest_enforces_entry_and_regular_byte_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="200000 entries"):
        recovery._manifest([_entry(path="a"), _entry(path="b")])
    monkeypatch.setattr(recovery, "MAX_REGULAR_BYTES", 2)
    with pytest.raises(ValueError, match="8589934592 regular bytes"):
        recovery._manifest([_entry(size=3)])


@pytest.mark.parametrize("field,value", [("path", "e\u0301"), ("target", "e\u0301")])
def test_non_nfc_names_and_targets_reject(field: str, value: str) -> None:
    assert unicodedata.normalize("NFC", value) != value
    if field == "path":
        with pytest.raises(ValueError, match="not NFC"):
            recovery._relative_path(value)
    else:
        with pytest.raises(ValueError, match="not NFC"):
            recovery._text(value, "symlink target")


def test_capture_value_is_frozen_closed_and_uses_fixed_relative_name() -> None:
    capture = ModuleArchiveCapture(
        manifest="sha256:" + "0" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256="sha256:" + "1" * 64,
    )
    assert capture.archive_filename == "modules.tar"
    with pytest.raises(ValidationError):
        ModuleArchiveCapture.model_validate({**capture.model_dump(), "archive_filename": "../x"})
    with pytest.raises(ValidationError):
        ModuleArchiveCapture.model_validate({**capture.model_dump(), "extra": True})


def test_sink_publishes_owner_only_archive_and_rejects_reuse(tmp_path: Path) -> None:
    directory_fd = _directory_fd(tmp_path)
    sink = RecoveryArchiveSink(directory_fd)
    os.close(directory_fd)

    sink.publish([b"ab", b"cd"])

    archive = tmp_path / "modules.tar"
    assert archive.read_bytes() == b"abcd"
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert not (tmp_path / ".modules.tar.partial").exists()
    with pytest.raises(ValueError, match="already used"):
        sink.publish([b"again"])


def test_sink_failure_removes_partial_and_never_replaces_archive(tmp_path: Path) -> None:
    directory_fd = _directory_fd(tmp_path)
    sink = RecoveryArchiveSink(directory_fd)
    os.close(directory_fd)

    def fail() -> Any:
        yield b"partial"
        raise OSError("injected write failure")

    with pytest.raises(OSError, match="injected"):
        sink.publish(fail())
    assert list(tmp_path.iterdir()) == []


def test_source_reads_exact_owner_file_once_and_closes_descriptors(tmp_path: Path) -> None:
    archive = tmp_path / "modules.tar"
    archive.write_bytes(b"abc")
    archive.chmod(0o600)
    capture = _capture(b"abc", [])
    directory_fd = _directory_fd(tmp_path)
    source = RecoveryArchiveSource(directory_fd)
    os.close(directory_fd)

    with source.open(capture) as stream:
        assert stream.read() == b"abc"
    with pytest.raises(ValueError, match="already used"), source.open(capture):
        pass


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o700])
def test_source_rejects_non_private_archive_mode(tmp_path: Path, mode: int) -> None:
    archive = tmp_path / "modules.tar"
    archive.write_bytes(b"abc")
    archive.chmod(mode)
    directory_fd = _directory_fd(tmp_path)
    source = RecoveryArchiveSource(directory_fd)
    os.close(directory_fd)
    with pytest.raises(ValueError, match="ownership or mode"), source.open(_capture(b"abc", [])):
        pass


def test_source_rejects_symlink_without_reading_outside(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    (tmp_path / "modules.tar").symlink_to(outside)
    directory_fd = _directory_fd(tmp_path)
    source = RecoveryArchiveSource(directory_fd)
    os.close(directory_fd)
    with pytest.raises(OSError), source.open(_capture(b"secret", [])):
        pass
    assert outside.read_bytes() == b"secret"


def test_source_rejects_wrong_service_owner_before_read(tmp_path: Path) -> None:
    archive = tmp_path / "modules.tar"
    archive.write_bytes(b"abc")
    archive.chmod(0o600)
    directory_fd = _directory_fd(tmp_path)
    with pytest.raises(ValueError, match="directory ownership or mode"):
        RecoveryArchiveSource(directory_fd, service_uid=os.geteuid() + 1)
    os.close(directory_fd)


def test_capabilities_reject_writable_owner_directory(tmp_path: Path) -> None:
    tmp_path.chmod(0o770)
    directory_fd = _directory_fd(tmp_path)
    with pytest.raises(ValueError, match="directory ownership or mode"):
        RecoveryArchiveSink(directory_fd)
    with pytest.raises(ValueError, match="directory ownership or mode"):
        RecoveryArchiveSource(directory_fd)
    os.close(directory_fd)


def test_source_enforces_archive_bound_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "modules.tar"
    archive.write_bytes(b"abcd")
    archive.chmod(0o600)
    monkeypatch.setattr(recovery, "MAX_ARCHIVE_BYTES", 3)
    directory_fd = _directory_fd(tmp_path)
    source = RecoveryArchiveSource(directory_fd)
    os.close(directory_fd)
    with pytest.raises(ValueError, match="byte reservation"), source.open(_capture(b"abcd", [])):
        pass


class _AbsentGuest:
    def __init__(self, *, exists: bool = False) -> None:
        self.present = exists
        self.calls: list[tuple[object, ...]] = []

    def exists(self, path: str) -> int:
        self.calls.append(("exists", path))
        return int(self.present)

    def rm_rf(self, path: str) -> None:
        self.calls.append(("rm_rf", path))

    def sync(self) -> None:
        self.calls.append(("sync",))

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))

    def close(self) -> None:
        self.calls.append(("close",))


def test_capture_and_observe_record_explicit_absence() -> None:
    guests: list[_AbsentGuest] = []

    def open_guest(_overlay: str) -> Any:
        guest = _AbsentGuest()
        guests.append(guest)
        return guest

    writer = RealGuestRecoveryWriter(open_guest)
    assert isinstance(writer.observe("overlay", "6.1"), AbsentComponentState)


def test_absent_capture_closes_sink_without_publication(tmp_path: Path) -> None:
    guest = _AbsentGuest()
    directory_fd = _directory_fd(tmp_path)
    sink = RecoveryArchiveSink(directory_fd)
    os.close(directory_fd)

    def open_guest(_overlay: str) -> Any:
        return guest

    capture = RealGuestRecoveryWriter(open_guest).capture("overlay", "6.1", sink)
    assert isinstance(capture, AbsentModuleCapture)
    assert sink._closed is True
    assert list(tmp_path.iterdir()) == []


class _XattrGuest:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls = 0

    def lgetxattrs(self, _path: str) -> list[dict[str, str | bytes]]:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, list)
        return cast(list[dict[str, str | bytes]], result)


def test_unsupported_xattrs_remain_unsupported_without_reprobing() -> None:
    guest = _XattrGuest([NotImplementedError(), [{"attrname": "security.x", "attrval": b"x"}]])
    assert recovery._read_xattrs(cast(Any, guest), "/first", None) == ({}, False)
    assert recovery._read_xattrs(cast(Any, guest), "/second", False) == ({}, False)
    assert guest.calls == 1


def test_xattr_failure_after_support_is_unreadable() -> None:
    guest = _XattrGuest([[], NotImplementedError()])
    assert recovery._read_xattrs(cast(Any, guest), "/first", None) == ({}, True)
    with pytest.raises(ValueError, match="became unreadable"):
        recovery._read_xattrs(cast(Any, guest), "/second", True)


def test_absent_restore_never_opens_archive_source(tmp_path: Path) -> None:
    guest = _AbsentGuest(exists=True)
    directory_fd = _directory_fd(tmp_path)
    source = RecoveryArchiveSource(directory_fd)
    os.close(directory_fd)

    def open_guest(_overlay: str) -> Any:
        return guest

    result = RealGuestRecoveryWriter(open_guest).restore(
        "overlay", "6.1", AbsentModuleCapture(), source
    )

    assert result == "absent"
    assert ("rm_rf", "/lib/modules/6.1") in guest.calls
    assert source._used is False
    assert source._closed is True


def test_restore_rejects_archive_digest_before_guest_mutation(tmp_path: Path) -> None:
    archive = tmp_path / "modules.tar"
    archive.write_bytes(b"not the captured archive")
    archive.chmod(0o600)
    directory_fd = _directory_fd(tmp_path)
    source = RecoveryArchiveSource(directory_fd)
    os.close(directory_fd)
    opened = False

    def forbidden(_overlay: str) -> Any:
        nonlocal opened
        opened = True
        raise AssertionError("guest must not open")

    capture = ModuleArchiveCapture(
        manifest="sha256:" + "0" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256="sha256:" + "1" * 64,
    )
    with pytest.raises(ValueError, match="digest does not match"):
        RealGuestRecoveryWriter(forbidden).restore("overlay", "6.1", capture, source)
    assert opened is False


def test_restore_rejects_manifest_mismatch_before_staging(tmp_path: Path) -> None:
    entries = [_entry()]
    archive = recovery._archive(entries, {"kernel.ko": b"abc"})
    path = tmp_path / "modules.tar"
    path.write_bytes(archive)
    path.chmod(0o600)
    capture = _capture(archive, entries).model_copy(update={"manifest": "sha256:" + "f" * 64})
    staged = False

    class RecordingWriter(RealGuestRecoveryWriter):
        def _stage(self, *args: object, **kwargs: object) -> None:
            nonlocal staged
            staged = True

    directory_fd = _directory_fd(tmp_path)
    source = RecoveryArchiveSource(directory_fd)
    os.close(directory_fd)
    with pytest.raises(ValueError, match="manifest does not match"):
        RecordingWriter().restore("overlay", "6.1", capture, source)
    assert staged is False


def test_restore_signature_is_capture_then_source() -> None:
    parameters = list(inspect.signature(RealGuestRecoveryWriter.restore).parameters)
    assert parameters == ["self", "overlay", "release", "capture", "source"]


class _RenameFailureGuest(_AbsentGuest):
    def __init__(self) -> None:
        super().__init__()
        self.paths = {"/lib/modules/6.1"}
        self.failed = False

    def exists(self, path: str) -> int:
        return int(path in self.paths)

    def ls(self, _path: str) -> list[str]:
        return []

    def mkdir_p(self, path: str) -> None:
        self.paths.add(path)

    def mv(self, source: str, target: str) -> None:
        self.calls.append(("mv", source, target))
        if source.endswith(".kdive-partial") and not self.failed:
            self.failed = True
            raise OSError("injected publication failure")
        self.paths.remove(source)
        self.paths.add(target)

    def rm_rf(self, path: str) -> None:
        self.calls.append(("rm_rf", path))
        self.paths.discard(path)


def test_failed_live_rename_restores_previous_tree_and_cleans_partial() -> None:
    guest = _RenameFailureGuest()

    def open_guest(_overlay: str) -> Any:
        return guest

    writer = RealGuestRecoveryWriter(open_guest)
    with pytest.raises(OSError, match="publication failure"):
        writer._stage("overlay", "6.1", [], {}, recovery._manifest([])[1])

    assert guest.paths == {"/lib/modules/6.1"}
    assert (
        "mv",
        "/lib/modules/6.1.kdive-previous",
        "/lib/modules/6.1",
    ) in guest.calls


def test_existing_owned_partial_rejects_before_guest_mutation() -> None:
    guest = _RenameFailureGuest()
    guest.paths.add("/lib/modules/6.1.kdive-previous")

    def open_guest(_overlay: str) -> Any:
        return guest

    before = set(guest.paths)
    with pytest.raises(ValueError, match="requires classification"):
        RealGuestRecoveryWriter(open_guest)._stage(
            "overlay", "6.1", [], {}, recovery._manifest([])[1]
        )
    assert guest.paths == before
    assert not any(call[0] == "mv" for call in guest.calls)
