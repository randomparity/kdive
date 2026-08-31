"""Task 2 contract tests for pure module-tree recovery I/O."""

from __future__ import annotations

import hashlib
import inspect
import io
import os
import stat
import tarfile
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

import pytest

from kdive.providers.local_libvirt.lifecycle.boot import recovery
from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    AbsentModuleCapture,
    GuestTreeEntry,
    KernelBundleSource,
    ModuleArchiveCapture,
    RealGuestRecoveryWriter,
    RecoveryArchiveSink,
    RecoveryArchiveSource,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    PresentComponentState,
)

SYSTEM = "00000000-0000-0000-0000-000000000001"
RUN = "00000000-0000-0000-0000-000000000002"
ACTIVATION = "00000000-0000-0000-0000-000000000003"
RELEASE = "6.12.0-test"
BINDING = ExternalBootActivationBinding(system_id=SYSTEM, run_id=RUN, activation_id=ACTIVATION)


def _entry(path: str, content: bytes | None = None, **changes: object) -> GuestTreeEntry:
    values: dict[str, object] = {
        "path": path,
        "kind": "regular" if content is not None else "directory",
        "mode": "0644" if content is not None else "0755",
        "uid": 0,
        "gid": 0,
        "size": len(content or b""),
        "target": None,
        "xattrs_supported": True,
        "xattrs": {},
    }
    values.update(changes)
    return GuestTreeEntry.model_validate(values)


class FakeTree:
    def __init__(
        self,
        entries: list[GuestTreeEntry] | None = None,
        contents: dict[str, bytes] | None = None,
        *,
        binding: ExternalBootActivationBinding = BINDING,
        release: str = RELEASE,
        mutable: bool = False,
        root: Literal["absent", "directory", "other"] = "directory",
    ) -> None:
        self.binding = binding
        self.release = release
        self.mutable = mutable
        self.root = root
        self._entries = entries or []
        self.contents = contents or {}
        self.writes: list[tuple[str, str]] = []
        self.opened: list[str] = []

    def root_kind(self) -> Literal["absent", "directory", "other"]:
        return self.root

    def entries(self) -> Iterator[GuestTreeEntry]:
        yield from self._entries

    @contextmanager
    def open_regular(self, path: str, size: int) -> Iterator[BinaryIO]:
        self.opened.append(path)
        value = self.contents[path]
        if len(value) != size:
            raise ValueError("substitution detected")
        yield io.BytesIO(value)

    def create_directory(self, entry: GuestTreeEntry) -> None:
        self.writes.append(("directory", entry.path))

    def create_regular(self, entry: GuestTreeEntry, content: BinaryIO) -> None:
        self.writes.append(("regular", entry.path))
        self.contents[entry.path] = content.read()

    def create_symlink(self, entry: GuestTreeEntry) -> None:
        self.writes.append(("symlink", entry.path))

    def remove_all(self) -> None:
        self.writes.append(("remove", ""))


def _dir_fd(path: Path) -> int:
    path.chmod(0o700)
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _file_fd(path: Path, value: bytes) -> int:
    path.write_bytes(value)
    path.chmod(0o600)
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sink(path: Path, binding: ExternalBootActivationBinding = BINDING) -> RecoveryArchiveSink:
    fd = _dir_fd(path)
    try:
        return RecoveryArchiveSink(fd, binding=binding, release=RELEASE)
    finally:
        os.close(fd)


def _source(
    path: Path,
    capture: ModuleArchiveCapture,
    binding: ExternalBootActivationBinding = BINDING,
) -> RecoveryArchiveSource:
    fd = _dir_fd(path)
    try:
        return RecoveryArchiveSource(fd, binding=binding, release=RELEASE, capture=capture)
    finally:
        os.close(fd)


def _bundle(
    path: Path,
    value: bytes,
    binding: ExternalBootActivationBinding = BINDING,
) -> KernelBundleSource:
    fd = _file_fd(path, value)
    try:
        return KernelBundleSource(
            fd, binding=binding, release=RELEASE, size=len(value), digest=_digest(value)
        )
    finally:
        os.close(fd)


def test_empty_manifest_frozen_vector() -> None:
    payload, digest, count, size = recovery._manifest([])
    assert payload == b'{"entries":[],"schema":"recovery-module-tree-v1"}'
    assert digest == "sha256:7048c9e065ecf77a964188f42aaebb79a3e8238ecc47736ae47239b8ceec30a5"
    assert (count, size) == (0, 0)


def test_capture_and_observe_have_identical_regular_manifest(tmp_path: Path) -> None:
    entry = _entry(
        "kernel.ko",
        b"abc",
        mode="0600",
        uid=12,
        gid=34,
        xattrs={"security.selinux": b"label\0", "system.posix_acl_access": b"acl"},
    )
    tree = FakeTree([entry], {"kernel.ko": b"abc"})
    capture = RealGuestRecoveryWriter().capture(tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    observed = RealGuestRecoveryWriter().observe(tree, RELEASE)
    assert isinstance(observed, PresentComponentState)
    assert observed.manifest == capture.manifest
    assert capture.entry_count == 1 and capture.uncompressed_bytes == 3
    assert capture.archive_bytes == (tmp_path / "modules.tar").stat().st_size


@pytest.mark.parametrize("architecture", ["x86_64", "ppc64le"])
def test_regular_manifest_is_cross_arch_canonical_vector(architecture: str) -> None:
    del architecture
    entry = recovery._ManifestEntry(
        path="kernel.ko",
        kind="regular",
        mode="0600",
        uid=12,
        gid=34,
        size=3,
        sha256=_digest(b"abc"),
        target=None,
        xattrs_supported=True,
        xattrs={"security.selinux": "bGFiZWwA", "system.posix_acl_access": "YWNs"},
    )
    payload, digest, _, _ = recovery._manifest([entry])
    assert payload == (
        b'{"entries":[{"gid":34,"kind":"regular","mode":"0600",'
        b'"path":"kernel.ko","sha256":"sha256:ba7816bf8f01cfea414140de5dae2223'
        b'b00361a396177a9cb410ff61f20015ad","size":3,"target":null,"uid":12,'
        b'"xattrs":{"security.selinux":"bGFiZWwA","system.posix_acl_access":"YWNs"},'
        b'"xattrs_supported":true}],"schema":"recovery-module-tree-v1"}'
    )
    assert digest == "sha256:582f0ef670052bf18700e453a9357eed5ce7d385940bf21e7330d30ce7e93e3b"


def test_absolute_symlink_and_unsupported_xattrs_have_golden_metadata(tmp_path: Path) -> None:
    symlink = _entry(
        "build",
        kind="symlink",
        mode="0777",
        target="/usr/src/kernels/current",
        xattrs_supported=False,
    )
    capture = RealGuestRecoveryWriter().capture(FakeTree([symlink]), RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    with tarfile.open(tmp_path / "modules.tar") as archive:
        member = archive.getmember("build")
        assert member.linkname == "/usr/src/kernels/current"
        assert member.pax_headers["KDIVE.xattrs-supported"] == "0"


def test_absent_capture_observe_and_restore_do_not_read_source(tmp_path: Path) -> None:
    reader = RealGuestRecoveryWriter()
    absent = FakeTree(root="absent")
    sink = _sink(tmp_path)
    assert isinstance(reader.capture(absent, RELEASE, sink), AbsentModuleCapture)
    assert isinstance(reader.observe(absent, RELEASE), AbsentComponentState)
    mutable = FakeTree(mutable=True)
    capture = ModuleArchiveCapture(
        manifest="sha256:" + "0" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256=_digest(b""),
        archive_bytes=0,
    )
    (tmp_path / "modules.tar").write_bytes(b"")
    (tmp_path / "modules.tar").chmod(0o600)
    source = _source(tmp_path, capture)
    assert reader.restore(mutable, RELEASE, AbsentModuleCapture(), source) == "absent"
    assert mutable.writes == [("remove", "")]
    assert source._used is False and source._closed is True


@pytest.mark.parametrize("path", ["/x", "../x", "a/../x", "a//x", "e\u0301"])
def test_tree_rejects_path_escape_and_non_nfc_before_content_read(path: str) -> None:
    tree = FakeTree([_entry(path, b"x")], {path: b"x"})
    with pytest.raises(ValueError, match="canonical|NFC"):
        RealGuestRecoveryWriter().observe(tree, RELEASE)
    assert tree.opened == []


@pytest.mark.parametrize("root", ["other"])
def test_tree_rejects_non_directory_root_before_entry_read(
    root: Literal["other"],
) -> None:
    tree = FakeTree([_entry("x", b"x")], {"x": b"x"}, root=root)
    with pytest.raises(ValueError, match="root is not a directory"):
        RealGuestRecoveryWriter().observe(tree, RELEASE)
    assert tree.opened == []


def test_tree_rejects_hardlink_duplicate_special_and_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = RealGuestRecoveryWriter()
    with pytest.raises(ValueError, match="Hard-linked|hard-linked"):
        writer.observe(FakeTree([_entry("x", b"x", link_count=2)], {"x": b"x"}), RELEASE)
    with pytest.raises(ValueError, match="duplicate"):
        writer.observe(FakeTree([_entry("x"), _entry("x")]), RELEASE)
    with pytest.raises(ValueError):
        GuestTreeEntry.model_validate({**_entry("x").model_dump(), "kind": "socket"})
    monkeypatch.setattr(recovery, "MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="200000"):
        writer.observe(FakeTree([_entry("a"), _entry("b")]), RELEASE)
    monkeypatch.setattr(recovery, "MAX_REGULAR_BYTES", 1)
    with pytest.raises(ValueError, match="8589934592"):
        writer.observe(FakeTree([_entry("x", b"xx")], {"x": b"xx"}), RELEASE)


def test_content_substitution_is_detected_by_capability_read() -> None:
    tree = FakeTree([_entry("x", b"abc")], {"x": b"changed"})
    with pytest.raises(ValueError, match="substitution"):
        RealGuestRecoveryWriter().observe(tree, RELEASE)


def test_wrong_sink_owner_release_and_access_mode_are_zero_read(tmp_path: Path) -> None:
    other = BINDING.model_copy(update={"activation_id": "00000000-0000-0000-0000-000000000099"})
    tree = FakeTree([_entry("x", b"x")], {"x": b"x"})
    with pytest.raises(ValueError, match="sink owner"):
        RealGuestRecoveryWriter().capture(tree, RELEASE, _sink(tmp_path, other))
    assert tree.opened == []
    with pytest.raises(ValueError, match="release"):
        RealGuestRecoveryWriter().observe(tree, "wrong")
    with pytest.raises(ValueError, match="access mode"):
        RealGuestRecoveryWriter().observe(FakeTree(mutable=True), RELEASE)


def test_source_rejects_symlink_mode_directory_owner_and_wrong_capture(tmp_path: Path) -> None:
    capture = ModuleArchiveCapture(
        manifest="sha256:" + "0" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256=_digest(b"abc"),
        archive_bytes=3,
    )
    outside = tmp_path / "outside"
    outside.write_bytes(b"abc")
    (tmp_path / "modules.tar").symlink_to(outside)
    with pytest.raises(OSError):
        _source(tmp_path, capture)
    (tmp_path / "modules.tar").unlink()
    (tmp_path / "modules.tar").write_bytes(b"abc")
    (tmp_path / "modules.tar").chmod(0o640)
    with pytest.raises(ValueError, match="mode"):
        _source(tmp_path, capture)
    (tmp_path / "modules.tar").chmod(0o600)
    tmp_path.chmod(0o770)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    with pytest.raises(ValueError, match="root ownership"):
        RecoveryArchiveSource(directory_fd, binding=BINDING, release=RELEASE, capture=capture)
    os.close(directory_fd)


def test_recovery_source_exact_identity_rejects_before_tree_write(tmp_path: Path) -> None:
    source_tree = FakeTree([_entry("x", b"x")], {"x": b"x"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    wrong = capture.model_copy(update={"manifest": "sha256:" + "f" * 64})
    target = FakeTree(mutable=True)
    source = _source(tmp_path, capture)
    with pytest.raises(ValueError, match="identity"):
        RealGuestRecoveryWriter().restore(target, RELEASE, wrong, source)
    assert target.writes == [] and source._used is False and source._closed is True


def test_restore_success_and_source_reuse_rejection(tmp_path: Path) -> None:
    source_tree = FakeTree([_entry("x", b"abc")], {"x": b"abc"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    source = _source(tmp_path, capture)
    target = FakeTree(mutable=True)
    assert RealGuestRecoveryWriter().restore(target, RELEASE, capture, source) == capture.manifest
    assert target.contents["x"] == b"abc"
    with pytest.raises(ValueError, match="not reusable"), source.stream():
        pass


def test_kernel_source_install_success_owner_and_digest_failures(tmp_path: Path) -> None:
    source_tree = FakeTree([_entry("x", b"abc")], {"x": b"abc"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    archive = (tmp_path / "modules.tar").read_bytes()
    target = FakeTree(mutable=True)
    RealGuestRecoveryWriter().install(target, RELEASE, _bundle(tmp_path / "bundle", archive))
    assert target.contents["x"] == b"abc"
    other = BINDING.model_copy(update={"system_id": "00000000-0000-0000-0000-000000000099"})
    target = FakeTree(mutable=True)
    with pytest.raises(ValueError, match="owner or release"):
        RealGuestRecoveryWriter().install(
            target, RELEASE, _bundle(tmp_path / "other", archive, other)
        )
    assert target.writes == []
    bad = tmp_path / "bad"
    fd = _file_fd(bad, archive)
    try:
        source = KernelBundleSource(
            fd, binding=BINDING, release=RELEASE, size=len(archive), digest="sha256:" + "f" * 64
        )
    finally:
        os.close(fd)
    with pytest.raises(ValueError, match="digest"):
        RealGuestRecoveryWriter().install(FakeTree(mutable=True), RELEASE, source)


def test_signatures_and_capability_surface_exclude_paths_and_publication() -> None:
    assert list(inspect.signature(RealGuestRecoveryWriter.restore).parameters) == [
        "self",
        "tree",
        "release",
        "capture",
        "source",
    ]
    assert list(inspect.signature(RealGuestRecoveryWriter.install).parameters) == [
        "self",
        "tree",
        "release",
        "source",
    ]
    forbidden = {"overlay", "path", "rename", "fsync", "phase", "restart", "mv"}
    assert forbidden.isdisjoint(FakeTree.__dict__)


def test_sink_streams_and_cleans_partial_on_failure(tmp_path: Path) -> None:
    sink = _sink(tmp_path)

    class Failure(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            if self.tell():
                raise OSError("injected read")
            return super().read(1)

    with pytest.raises(OSError, match="injected read"):
        sink.publish(Failure(b"abc"))
    assert not (tmp_path / ".modules.tar.partial").exists()
    assert sink._closed is True


@pytest.mark.parametrize("existing", ["regular", "symlink"])
def test_sink_eexist_never_removes_an_unowned_partial(tmp_path: Path, existing: str) -> None:
    partial = tmp_path / ".modules.tar.partial"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    if existing == "regular":
        partial.write_bytes(b"other publisher")
    else:
        partial.symlink_to(outside)
    before = partial.lstat()

    with pytest.raises(FileExistsError):
        _sink(tmp_path).publish(io.BytesIO(b"ours"))

    after = partial.lstat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert outside.read_bytes() == b"outside"


def test_sink_cleanup_rechecks_created_inode_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery._copy_to_fd

    def replace_then_fail(source: BinaryIO, fd: int, limit: int) -> tuple[str, int]:
        del source, fd, limit
        partial = tmp_path / ".modules.tar.partial"
        partial.unlink()
        partial.write_bytes(b"replacement")
        raise OSError("injected publisher failure")

    monkeypatch.setattr(recovery, "_copy_to_fd", replace_then_fail)
    with pytest.raises(OSError, match="publisher failure"):
        _sink(tmp_path).publish(io.BytesIO(b"ours"))
    monkeypatch.setattr(recovery, "_copy_to_fd", original)
    assert (tmp_path / ".modules.tar.partial").read_bytes() == b"replacement"


def test_sink_publishers_hold_one_directory_lock_through_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    second_opened = threading.Event()
    calls: list[bytes] = []
    original = recovery._copy_to_fd
    original_open = os.open

    def controlled(source: BinaryIO, fd: int, limit: int) -> tuple[str, int]:
        value = source.read()
        calls.append(value)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return original(io.BytesIO(value), fd, limit)

    monkeypatch.setattr(recovery, "_copy_to_fd", controlled)

    def tracked_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        is_second_partial = threading.current_thread().name == "second-publisher" and os.fspath(
            path
        ).startswith(".")
        if is_second_partial:
            second_opened.set()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", tracked_open)
    errors: list[BaseException] = []

    def publish(value: bytes) -> None:
        try:
            _sink(tmp_path).publish(io.BytesIO(value))
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=publish, args=(b"first",))
    second = threading.Thread(target=publish, args=(b"second",), name="second-publisher")
    first.start()
    assert entered.wait(2)
    second.start()
    assert not second_opened.wait(0.1)
    assert calls == [b"first"]
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert calls == [b"first", b"second"]
    assert (tmp_path / "modules.tar").read_bytes() == b"second"


def test_capability_constructor_failures_leave_descriptor_count_unchanged(tmp_path: Path) -> None:
    capture = ModuleArchiveCapture(
        manifest="sha256:" + "0" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256=_digest(b"x"),
        archive_bytes=1,
    )
    (tmp_path / "modules.tar").write_bytes(b"x")
    (tmp_path / "modules.tar").chmod(0o600)
    directory_fd = _dir_fd(tmp_path)
    bundle_fd = _file_fd(tmp_path / "bundle", b"x")
    try:
        before = len(list(Path("/proc/self/fd").iterdir()))
        for constructor in (
            lambda: RecoveryArchiveSink(directory_fd, binding=BINDING, release="../bad"),
            lambda: RecoveryArchiveSource(
                directory_fd, binding=BINDING, release="../bad", capture=capture
            ),
            lambda: KernelBundleSource(
                bundle_fd,
                binding=BINDING,
                release="../bad",
                size=1,
                digest=_digest(b"x"),
            ),
        ):
            with pytest.raises(ValueError, match="release"):
                constructor()
            assert len(list(Path("/proc/self/fd").iterdir())) == before
    finally:
        os.close(bundle_fd)
        os.close(directory_fd)


def test_recovery_source_open_and_validation_failures_close_all_descriptors(tmp_path: Path) -> None:
    capture = ModuleArchiveCapture(
        manifest="sha256:" + "0" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256=_digest(b"x"),
        archive_bytes=1,
    )
    directory_fd = _dir_fd(tmp_path)
    try:
        before = len(list(Path("/proc/self/fd").iterdir()))
        with pytest.raises(FileNotFoundError):
            RecoveryArchiveSource(directory_fd, binding=BINDING, release=RELEASE, capture=capture)
        assert len(list(Path("/proc/self/fd").iterdir())) == before
        (tmp_path / "modules.tar").write_bytes(b"xx")
        (tmp_path / "modules.tar").chmod(0o600)
        with pytest.raises(ValueError, match="size"):
            RecoveryArchiveSource(directory_fd, binding=BINDING, release=RELEASE, capture=capture)
        assert len(list(Path("/proc/self/fd").iterdir())) == before
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("kind", ["directory", "mode", "overbound"])
def test_source_and_kernel_reject_actual_file_shape_without_fd_leaks(
    tmp_path: Path, kind: str
) -> None:
    capture = ModuleArchiveCapture(
        manifest="sha256:" + "0" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256=_digest(b""),
        archive_bytes=0 if kind != "overbound" else recovery.MAX_ARCHIVE_BYTES,
    )
    archive = tmp_path / "modules.tar"
    bundle = tmp_path / "bundle"
    if kind == "directory":
        archive.mkdir()
        bundle.mkdir()
    else:
        archive.touch(mode=0o600)
        bundle.touch(mode=0o600)
        if kind == "mode":
            archive.chmod(0o640)
            bundle.chmod(0o640)
        else:
            with archive.open("wb") as stream:
                stream.truncate(recovery.MAX_ARCHIVE_BYTES + 1)
            with bundle.open("wb") as stream:
                stream.truncate(recovery.MAX_ARCHIVE_BYTES + 1)
            capture = capture.model_copy(update={"archive_bytes": recovery.MAX_ARCHIVE_BYTES})
    directory_fd = _dir_fd(tmp_path)
    bundle_fd = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = len(list(Path("/proc/self/fd").iterdir()))
        with pytest.raises(ValueError, match="regular|mode|size"):
            RecoveryArchiveSource(directory_fd, binding=BINDING, release=RELEASE, capture=capture)
        assert len(list(Path("/proc/self/fd").iterdir())) == before
        with pytest.raises(ValueError, match="regular|mode|size"):
            KernelBundleSource(
                bundle_fd,
                binding=BINDING,
                release=RELEASE,
                size=recovery.MAX_ARCHIVE_BYTES,
                digest=_digest(b""),
            )
        assert len(list(Path("/proc/self/fd").iterdir())) == before
    finally:
        os.close(bundle_fd)
        os.close(directory_fd)


@pytest.mark.parametrize(
    "fault", ["write", "file-close", "file-fsync", "rename", "dir-fsync", "lock", "unlock"]
)
def test_sink_syscall_failures_close_and_leave_only_durable_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    sink = _sink(tmp_path)
    original_write, original_fsync, original_close = os.write, os.fsync, os.close
    original_rename, original_flock = os.rename, recovery.fcntl.flock
    fsync_calls = 0
    close_failed = False

    def write(fd: int, value: bytes | memoryview) -> int:
        if fault == "write":
            raise OSError("injected write")
        return original_write(fd, value)

    def fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fault == "file-fsync" and fsync_calls == 1:
            raise OSError("injected file fsync")
        if fault == "dir-fsync" and fsync_calls == 2:
            raise OSError("injected directory fsync")
        original_fsync(fd)

    def rename(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if fault == "rename":
            raise OSError("injected rename")
        original_rename(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def flock(fd: int, operation: int) -> None:
        if fault == "lock" and operation == recovery.fcntl.LOCK_EX:
            raise OSError("injected lock")
        if fault == "unlock" and operation == recovery.fcntl.LOCK_UN:
            raise OSError("injected unlock")
        original_flock(fd, operation)

    def close(fd: int) -> None:
        nonlocal close_failed
        if fault == "file-close" and fd != sink._directory_fd and not close_failed:
            close_failed = True
            raise OSError("injected file close")
        original_close(fd)

    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(recovery.fcntl, "flock", flock)
    with pytest.raises(OSError, match="injected"):
        sink.publish(io.BytesIO(b"value"))
    assert sink._closed is True
    assert not (tmp_path / ".modules.tar.partial").exists()
    assert (tmp_path / "modules.tar").exists() is (fault in {"dir-fsync", "unlock"})


def test_sink_primary_error_survives_close_and_unlock_cleanup_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _sink(tmp_path)
    original_flock = recovery.fcntl.flock
    original_close = os.close
    failed_close = False

    def copy_failure(source: BinaryIO, fd: int, limit: int) -> tuple[str, int]:
        del source, fd, limit
        raise ValueError("primary publisher error")

    def flock(fd: int, operation: int) -> None:
        if operation == recovery.fcntl.LOCK_UN:
            raise OSError("unlock cleanup error")
        original_flock(fd, operation)

    def close(fd: int) -> None:
        nonlocal failed_close
        if fd == sink._directory_fd and not failed_close:
            failed_close = True
            raise OSError("close cleanup error")
        original_close(fd)

    monkeypatch.setattr(recovery, "_copy_to_fd", copy_failure)
    monkeypatch.setattr(recovery.fcntl, "flock", flock)
    monkeypatch.setattr(os, "close", close)
    with pytest.raises(ValueError, match="primary publisher error") as caught:
        sink.publish(io.BytesIO(b"value"))
    assert any("cleanup failed" in note for note in caught.value.__notes__)
    monkeypatch.setattr(recovery.fcntl, "flock", original_flock)
    sink.close()
    assert sink._closed is True


@pytest.mark.parametrize("field", ["mode", "uid", "gid"])
def test_hostile_later_archive_shape_rejects_on_public_install_before_writes(
    tmp_path: Path, field: str
) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in ("valid", "later"):
            member = tarfile.TarInfo(path)
            member.size = 1
            member.pax_headers = {"KDIVE.xattrs-supported": "1"}
            if path == "later":
                setattr(member, field, -1 if field != "mode" else 0o10000)
            archive.addfile(member, io.BytesIO(b"x"))
    if field == "mode":
        raw = bytearray(output.getvalue())
        offset = next(
            index for index in range(0, len(raw), 512) if raw[index : index + 6] == b"later\0"
        )
        raw[offset + 100 : offset + 108] = b"00010000"
        raw[offset + 148 : offset + 156] = b"        "
        checksum = sum(raw[offset : offset + 512])
        raw[offset + 148 : offset + 156] = f"{checksum:06o}\0 ".encode()
        output = io.BytesIO(raw)
    target = FakeTree(mutable=True)
    with pytest.raises(ValueError, match="canonical range|owner id"):
        RealGuestRecoveryWriter().install(
            target, RELEASE, _bundle(tmp_path / f"bad-{field}", output.getvalue())
        )
    assert target.writes == []


def test_capture_rolls_regular_content_to_disk_and_bounds_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = b"x" * (recovery._COPY_CHUNK + 1)
    tree = FakeTree([_entry("large", value)], {"large": value})
    original = tempfile.SpooledTemporaryFile
    rolled: list[bool] = []

    @contextmanager
    def tracked_spool(max_size: int = 0) -> Iterator[BinaryIO]:
        with original(max_size=max_size) as stream:
            yield cast(BinaryIO, stream)
            rolled.append(bool(cast(Any, stream)._rolled))

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", tracked_spool)
    capture = RealGuestRecoveryWriter().capture(tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    assert rolled == [True]


def test_tree_xattr_support_mismatch_rejects_before_read_or_publish(tmp_path: Path) -> None:
    tree = FakeTree(
        [_entry("x", b"x", xattrs_supported=False, xattrs={"security.x": b"value"})],
        {"x": b"x"},
    )
    with pytest.raises(ValueError, match="xattrs while support is false"):
        RealGuestRecoveryWriter().capture(tree, RELEASE, _sink(tmp_path))
    assert tree.opened == []
    assert not (tmp_path / "modules.tar").exists()


def test_tree_population_stops_after_first_write_failure(tmp_path: Path) -> None:
    source_tree = FakeTree([_entry("a", b"a"), _entry("b", b"b")], {"a": b"a", "b": b"b"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)

    class FailingTree(FakeTree):
        def create_regular(self, entry: GuestTreeEntry, content: BinaryIO) -> None:
            self.writes.append(("regular", entry.path))
            raise OSError("injected guest write")

    target = FailingTree(mutable=True)
    with pytest.raises(OSError, match="guest write"):
        RealGuestRecoveryWriter().restore(target, RELEASE, capture, _source(tmp_path, capture))
    assert target.writes == [("regular", "a")]


def test_tree_population_stops_after_second_write_failure(tmp_path: Path) -> None:
    source_tree = FakeTree(
        [_entry(name, name.encode()) for name in ("a", "b", "c")],
        {name: name.encode() for name in ("a", "b", "c")},
    )
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)

    class SecondWriteFails(FakeTree):
        def create_regular(self, entry: GuestTreeEntry, content: BinaryIO) -> None:
            self.writes.append(("regular", entry.path))
            if len(self.writes) == 2:
                raise OSError("injected second guest write")
            self.contents[entry.path] = content.read()

    target = SecondWriteFails(mutable=True)
    with pytest.raises(OSError, match="second guest write"):
        RealGuestRecoveryWriter().restore(target, RELEASE, capture, _source(tmp_path, capture))
    assert target.writes == [("regular", "a"), ("regular", "b")]


def test_open_sources_detect_truncation_and_close_after_failed_use(tmp_path: Path) -> None:
    source_tree = FakeTree([_entry("x", b"abc")], {"x": b"abc"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    recovery_source = _source(tmp_path, capture)
    (tmp_path / "modules.tar").write_bytes(b"")
    with pytest.raises(ValueError, match="ended before"):
        RealGuestRecoveryWriter().restore(FakeTree(mutable=True), RELEASE, capture, recovery_source)
    assert recovery_source._closed is True

    bundle_path = tmp_path / "bundle-truncated"
    bundle = _bundle(bundle_path, b"abc")
    bundle_path.write_bytes(b"")
    with pytest.raises(ValueError, match="ended before"):
        RealGuestRecoveryWriter().install(FakeTree(mutable=True), RELEASE, bundle)
    assert bundle._closed is True


@pytest.mark.parametrize("limit", ["entries", "bytes"])
def test_public_archive_limits_reject_before_tree_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in ("a", "b"):
            member = tarfile.TarInfo(name)
            member.size = 1
            member.pax_headers = {"KDIVE.xattrs-supported": "1"}
            archive.addfile(member, io.BytesIO(b"x"))
    monkeypatch.setattr(recovery, "MAX_ENTRIES" if limit == "entries" else "MAX_REGULAR_BYTES", 1)
    target = FakeTree(mutable=True)
    with pytest.raises(ValueError, match="exceeds"):
        RealGuestRecoveryWriter().install(
            target, RELEASE, _bundle(tmp_path / f"limit-{limit}", output.getvalue())
        )
    assert target.writes == []


def test_exact_entry_and_byte_bounds_without_materializing_entries() -> None:
    for count in range(1, recovery.MAX_ENTRIES + 1):
        recovery._bounded_entry_count(count, source="tree")
    with pytest.raises(ValueError, match="200000"):
        recovery._bounded_entry_count(recovery.MAX_ENTRIES + 1, source="archive")
    assert (
        recovery._bounded_regular_total(0, recovery.MAX_REGULAR_BYTES, source="tree")
        == recovery.MAX_REGULAR_BYTES
    )
    with pytest.raises(ValueError, match="8589934592"):
        recovery._bounded_regular_total(recovery.MAX_REGULAR_BYTES, 1, source="archive")


@pytest.mark.parametrize("source_kind", ["recovery", "kernel"])
def test_source_read_error_closes_and_rejects_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    source_tree = FakeTree([_entry("x", b"abc")], {"x": b"abc"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    archive = (tmp_path / "modules.tar").read_bytes()
    target = FakeTree(mutable=True)
    source: RecoveryArchiveSource | KernelBundleSource
    if source_kind == "recovery":
        source = _source(tmp_path, capture)
    else:
        source = _bundle(tmp_path / "read-error-bundle", archive)

    def fail_read(self: recovery._VerifiedReader, size: int = -1) -> bytes:
        del self, size
        raise OSError("injected ordinary read")

    monkeypatch.setattr(recovery._VerifiedReader, "read", fail_read)
    with pytest.raises(OSError, match="ordinary read"):
        if source_kind == "recovery":
            RealGuestRecoveryWriter().restore(
                target, RELEASE, capture, cast(RecoveryArchiveSource, source)
            )
        else:
            RealGuestRecoveryWriter().install(target, RELEASE, cast(KernelBundleSource, source))
    assert source._closed is True
    with pytest.raises(ValueError, match="not reusable"), source.stream():
        pass


@pytest.mark.parametrize("source_kind", ["recovery", "kernel"])
def test_source_close_failure_retains_error_and_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    source_tree = FakeTree([_entry("x", b"x")], {"x": b"x"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    before = len(list(Path("/proc/self/fd").iterdir()))
    source: RecoveryArchiveSource | KernelBundleSource
    source = (
        _source(tmp_path, capture)
        if source_kind == "recovery"
        else _bundle(tmp_path / "close-failure-bundle", (tmp_path / "modules.tar").read_bytes())
    )
    original_close = os.close
    failed = False

    def close(fd: int) -> None:
        nonlocal failed
        if fd == source._fd and not failed:
            failed = True
            raise OSError("injected source close")
        original_close(fd)

    monkeypatch.setattr(os, "close", close)
    with pytest.raises(OSError, match="source close"):
        source.close()
    source.close()
    assert source._closed is True
    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_recovery_source_directory_close_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = RealGuestRecoveryWriter().capture(
        FakeTree([_entry("x", b"x")], {"x": b"x"}), RELEASE, _sink(tmp_path)
    )
    assert isinstance(capture, ModuleArchiveCapture)
    before = len(list(Path("/proc/self/fd").iterdir()))
    source = _source(tmp_path, capture)
    original_close = os.close
    failed = False

    def close(fd: int) -> None:
        nonlocal failed
        if fd == source._directory_fd and not failed:
            failed = True
            raise OSError("injected directory close")
        original_close(fd)

    monkeypatch.setattr(os, "close", close)
    with pytest.raises(OSError, match="directory close"):
        source.close()
    source.close()
    assert source._closed and source._directory_closed
    assert len(list(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.parametrize("source_kind", ["recovery", "kernel"])
def test_sources_reject_reuse_after_success(tmp_path: Path, source_kind: str) -> None:
    source_tree = FakeTree([_entry("x", b"x")], {"x": b"x"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    source: RecoveryArchiveSource | KernelBundleSource
    if source_kind == "recovery":
        source = _source(tmp_path, capture)
        RealGuestRecoveryWriter().restore(FakeTree(mutable=True), RELEASE, capture, source)
    else:
        source = _bundle(tmp_path / "reuse-bundle", (tmp_path / "modules.tar").read_bytes())
        RealGuestRecoveryWriter().install(FakeTree(mutable=True), RELEASE, source)
    with pytest.raises(ValueError, match="not reusable"), source.stream():
        pass


@pytest.mark.parametrize("field", ["system_id", "run_id", "activation_id"])
def test_crossed_binding_fields_reject_both_sources_before_tree_write(
    tmp_path: Path, field: str
) -> None:
    source_tree = FakeTree([_entry("x", b"x")], {"x": b"x"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    wrong = BINDING.model_copy(update={field: "00000000-0000-0000-0000-000000000099"})
    archive = (tmp_path / "modules.tar").read_bytes()
    for source, operation in (
        (_source(tmp_path, capture, wrong), "restore"),
        (_bundle(tmp_path / f"cross-{field}", archive, wrong), "install"),
    ):
        target = FakeTree(mutable=True)
        with pytest.raises(ValueError, match="owner|identity"):
            if operation == "restore":
                RealGuestRecoveryWriter().restore(
                    target, RELEASE, capture, cast(RecoveryArchiveSource, source)
                )
            else:
                RealGuestRecoveryWriter().install(target, RELEASE, cast(KernelBundleSource, source))
        assert target.writes == []


def test_fifo_sources_are_rejected_without_descriptor_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    fifo_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
    directory_fd = _dir_fd(tmp_path)
    original_open = os.open
    capture = ModuleArchiveCapture(
        manifest="sha256:" + "0" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256=_digest(b""),
        archive_bytes=0,
    )

    def open_fifo(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "modules.tar":
            return os.dup(fifo_fd)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    try:
        before = len(list(Path("/proc/self/fd").iterdir()))
        monkeypatch.setattr(os, "open", open_fifo)
        with pytest.raises(ValueError, match="regular"):
            RecoveryArchiveSource(directory_fd, binding=BINDING, release=RELEASE, capture=capture)
        assert len(list(Path("/proc/self/fd").iterdir())) == before
        with pytest.raises(ValueError, match="regular"):
            KernelBundleSource(
                fifo_fd, binding=BINDING, release=RELEASE, size=0, digest=_digest(b"")
            )
        assert len(list(Path("/proc/self/fd").iterdir())) == before
    finally:
        os.close(directory_fd)
        os.close(fifo_fd)


def test_foreign_owned_sources_are_rejected_at_the_fstat_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "modules.tar").write_bytes(b"x")
    (tmp_path / "modules.tar").chmod(0o600)
    bundle_fd = _file_fd(tmp_path / "foreign-bundle", b"x")
    directory_fd = _dir_fd(tmp_path)
    capture = ModuleArchiveCapture(
        manifest="sha256:" + "0" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256=_digest(b"x"),
        archive_bytes=1,
    )
    original_fstat = os.fstat

    def foreign_regular(fd: int) -> os.stat_result:
        info = original_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            values = list(info)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return info

    try:
        before = len(list(Path("/proc/self/fd").iterdir()))
        monkeypatch.setattr(os, "fstat", foreign_regular)
        with pytest.raises(ValueError, match="ownership"):
            RecoveryArchiveSource(directory_fd, binding=BINDING, release=RELEASE, capture=capture)
        with pytest.raises(ValueError, match="ownership"):
            KernelBundleSource(
                bundle_fd, binding=BINDING, release=RELEASE, size=1, digest=_digest(b"x")
            )
        assert len(list(Path("/proc/self/fd").iterdir())) == before
    finally:
        os.close(directory_fd)
        os.close(bundle_fd)


def test_capture_propagates_capability_xattr_error_and_never_reads_symlink(
    tmp_path: Path,
) -> None:
    symlink = _entry("link", kind="symlink", target="/absolute", xattrs_supported=True)

    class XattrFailureTree(FakeTree):
        def entries(self) -> Iterator[GuestTreeEntry]:
            yield symlink
            raise OSError("injected supported xattr read")

    tree = XattrFailureTree()
    before = len(list(Path("/proc/self/fd").iterdir()))
    with pytest.raises(OSError, match="xattr read"):
        RealGuestRecoveryWriter().capture(tree, RELEASE, _sink(tmp_path))
    assert tree.opened == []
    assert len(list(Path("/proc/self/fd").iterdir())) == before
    assert not (tmp_path / "modules.tar").exists()


def test_open_capabilities_are_stable_across_path_replacement(tmp_path: Path) -> None:
    source_tree = FakeTree([_entry("x", b"x")], {"x": b"x"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    archive = (tmp_path / "modules.tar").read_bytes()
    recovery_source = _source(tmp_path, capture)
    (tmp_path / "modules.tar").unlink()
    (tmp_path / "modules.tar").write_bytes(b"replacement")
    target = FakeTree(mutable=True)
    RealGuestRecoveryWriter().restore(target, RELEASE, capture, recovery_source)
    assert target.contents["x"] == b"x"

    bundle_path = tmp_path / "stable-bundle"
    kernel_source = _bundle(bundle_path, archive)
    bundle_path.unlink()
    bundle_path.write_bytes(b"replacement")
    target = FakeTree(mutable=True)
    RealGuestRecoveryWriter().install(target, RELEASE, kernel_source)
    assert target.contents["x"] == b"x"


def test_capture_exact_entry_limit_is_accepted_and_plus_one_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recovery, "MAX_ENTRIES", 2)
    accepted = FakeTree([_entry("a"), _entry("b")])
    capture = RealGuestRecoveryWriter().capture(accepted, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture) and capture.entry_count == 2
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="200000"):
        RealGuestRecoveryWriter().capture(
            FakeTree([_entry("a"), _entry("b"), _entry("c")]), RELEASE, _sink(other)
        )
    assert not (other / "modules.tar").exists()


def test_complete_canonical_tar_bytes_and_digest_are_frozen(tmp_path: Path) -> None:
    tree = FakeTree(
        [_entry("x", b"x", xattrs={"a": b"b"})],
        {"x": b"x"},
    )
    capture = RealGuestRecoveryWriter().capture(tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    expected = bytearray(10_240)
    for offset, literal in (
        (0, b"././@PaxHeader"),
        (100, b"0000000"),
        (108, b"0000000"),
        (116, b"0000000"),
        (124, b"00000000060"),
        (136, b"00000000000"),
        (148, b"010211"),
        (155, b" x"),
        (257, b"ustar"),
        (263, b"00"),
        (512, b"28 KDIVE.xattrs-supported=1\n20 KDIVE.xattr.a=Yg\n"),
        (1024, b"x"),
        (1124, b"0000644"),
        (1132, b"0000000"),
        (1140, b"0000000"),
        (1148, b"00000000001"),
        (1160, b"00000000000"),
        (1172, b"006126"),
        (1179, b" 0"),
        (1281, b"ustar"),
        (1287, b"00"),
        (1536, b"x"),
    ):
        expected[offset : offset + len(literal)] = literal
    archive = (tmp_path / "modules.tar").read_bytes()
    assert archive == bytes(expected)
    assert capture.archive_sha256 == (
        "sha256:02e04db411bb09d5e4449af8250a7d81cbd2885f4e89fec3f93d062466004f14"
    )


def test_temporary_files_close_on_validation_and_population_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tempfile.TemporaryFile
    opened: list[BinaryIO] = []

    def tracked_temp() -> BinaryIO:
        stream = cast(BinaryIO, original())
        opened.append(stream)
        return stream

    monkeypatch.setattr(tempfile, "TemporaryFile", tracked_temp)
    invalid = io.BytesIO()
    with tarfile.open(fileobj=invalid, mode="w") as archive:
        member = tarfile.TarInfo("fifo")
        member.type = tarfile.FIFOTYPE
        archive.addfile(member)
    with pytest.raises(ValueError):
        RealGuestRecoveryWriter().install(
            FakeTree(mutable=True), RELEASE, _bundle(tmp_path / "invalid-temp", invalid.getvalue())
        )
    assert opened and all(stream.closed for stream in opened)

    opened.clear()
    source_tree = FakeTree([_entry("x", b"x")], {"x": b"x"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)

    class WriteFailure(FakeTree):
        def create_regular(self, entry: GuestTreeEntry, content: BinaryIO) -> None:
            del entry, content
            raise OSError("injected population")

    with pytest.raises(OSError, match="population"):
        RealGuestRecoveryWriter().restore(
            WriteFailure(mutable=True), RELEASE, capture, _source(tmp_path, capture)
        )
    assert opened and all(stream.closed for stream in opened)


def test_pax_xattrs_and_archive_digest_are_insertion_and_architecture_stable(
    tmp_path: Path,
) -> None:
    values = [("z.attr", b"z"), ("á.attr", b"a"), ("a.attr", b"first")]
    archives: list[bytes] = []
    digests: list[str] = []
    for index, architecture in enumerate(("x86_64", "ppc64le")):
        del architecture
        directory = tmp_path / str(index)
        directory.mkdir(mode=0o700)
        attrs = dict(values if index == 0 else reversed(values))
        tree = FakeTree([_entry("x", b"x", xattrs=attrs)], {"x": b"x"})
        capture = RealGuestRecoveryWriter().capture(tree, RELEASE, _sink(directory))
        assert isinstance(capture, ModuleArchiveCapture)
        archives.append((directory / "modules.tar").read_bytes())
        digests.append(capture.archive_sha256)
    assert archives[0] == archives[1]
    assert digests[0] == digests[1]


def _archive_with_later_member(*, mode: int = 0o644, pax: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        first = tarfile.TarInfo("valid")
        first.size = 1
        first.pax_headers = {"KDIVE.xattrs-supported": "1"}
        archive.addfile(first, io.BytesIO(b"v"))
        later = tarfile.TarInfo("later")
        later.mode = mode
        later.size = 1
        later.pax_headers = pax
        archive.addfile(later, io.BytesIO(b"x"))
    return output.getvalue()


@pytest.mark.parametrize(
    "archive",
    [
        _archive_with_later_member(
            pax={"KDIVE.xattrs-supported": "0", "KDIVE.xattr.security.x": "eA"}
        ),
        _archive_with_later_member(
            pax={"KDIVE.xattrs-supported": "1", "KDIVE.xattr.security.x": "eA=="}
        ),
        _archive_with_later_member(pax={"KDIVE.xattrs-supported": "1", "KDIVE.unknown": "x"}),
    ],
    ids=["xattrs-disabled", "padded-base64", "unknown-metadata"],
)
def test_hostile_later_metadata_is_rejected_before_any_tree_write(
    tmp_path: Path, archive: bytes
) -> None:
    target = FakeTree(mutable=True)
    with pytest.raises(ValueError):
        RealGuestRecoveryWriter().install(target, RELEASE, _bundle(tmp_path / "bundle", archive))
    assert target.writes == []


def test_archive_mode_and_owner_bounds_are_closed_before_writes() -> None:
    member = tarfile.TarInfo("later")
    member.pax_headers = {"KDIVE.xattrs-supported": "1"}
    for field, value in (("mode", 0o10000), ("uid", -1), ("gid", recovery.MAX_OWNER_ID + 1)):
        setattr(member, field, value)
        with pytest.raises(ValueError, match="canonical range|owner id"):
            recovery._manifest_from_member(member, "later", "regular", _digest(b""), 0)
        setattr(member, field, 0)


def test_recovery_source_retains_directory_and_file_descriptors_until_close(tmp_path: Path) -> None:
    source_tree = FakeTree([_entry("x", b"x")], {"x": b"x"})
    capture = RealGuestRecoveryWriter().capture(source_tree, RELEASE, _sink(tmp_path))
    assert isinstance(capture, ModuleArchiveCapture)
    before = len(list(Path("/proc/self/fd").iterdir()))
    source = _source(tmp_path, capture)
    retained = len(list(Path("/proc/self/fd").iterdir()))
    assert retained >= before + 2
    source.close()
    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_archive_rejects_hardlink_fifo_and_non_nfc_target_before_writes(tmp_path: Path) -> None:
    cases = [
        (tarfile.LNKTYPE, "x"),
        (tarfile.FIFOTYPE, ""),
        (tarfile.SYMTYPE, "e\u0301"),
    ]
    for kind, target in cases:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            member = tarfile.TarInfo("hostile")
            member.type, member.linkname = kind, target
            member.pax_headers = {"KDIVE.xattrs-supported": "1"}
            archive.addfile(member)
        value = buffer.getvalue()
        path = tmp_path / f"bad-{kind!r}"
        target_tree = FakeTree(mutable=True)
        with pytest.raises(ValueError, match="forbidden|NFC"):
            RealGuestRecoveryWriter().install(target_tree, RELEASE, _bundle(path, value))
        assert target_tree.writes == []


@pytest.mark.parametrize(
    "kind",
    [tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, b"s", b"V"],
)
def test_public_archive_rejects_every_forbidden_type_before_writes(
    tmp_path: Path, kind: bytes
) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("hostile")
        member.type = kind
        member.linkname = "target" if kind == tarfile.LNKTYPE else ""
        member.pax_headers = {"KDIVE.xattrs-supported": "1"}
        archive.addfile(member)
    target = FakeTree(mutable=True)
    with pytest.raises(ValueError, match="forbidden"):
        RealGuestRecoveryWriter().install(
            target, RELEASE, _bundle(tmp_path / f"type-{kind.hex()}", output.getvalue())
        )
    assert target.writes == []


def test_public_archive_duplicate_rejects_before_writes(tmp_path: Path) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for _ in range(2):
            member = tarfile.TarInfo("duplicate")
            member.pax_headers = {"KDIVE.xattrs-supported": "1"}
            archive.addfile(member)
    target = FakeTree(mutable=True)
    with pytest.raises(ValueError, match="duplicate"):
        RealGuestRecoveryWriter().install(
            target, RELEASE, _bundle(tmp_path / "duplicate", output.getvalue())
        )
    assert target.writes == []


def _replace_tar_header_bytes(data: bytes, name: bytes, offset: int, value: bytes) -> bytes:
    raw = bytearray(data)
    header = next(
        index for index in range(0, len(raw), 512) if raw[index : index + len(name)] == name
    )
    raw[header + offset : header + offset + len(value)] = value
    raw[header + 148 : header + 156] = b"        "
    checksum = sum(raw[header : header + 512])
    raw[header + 148 : header + 156] = f"{checksum:06o}\0 ".encode()
    return bytes(raw)


@pytest.mark.parametrize("field", ["name", "target"])
def test_public_archive_rejects_undecodable_header_text_before_writes(
    tmp_path: Path, field: str
) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "target"
        member.pax_headers = {"KDIVE.xattrs-supported": "1"}
        archive.addfile(member)
    archive_bytes = _replace_tar_header_bytes(
        output.getvalue(),
        b"link\0",
        0 if field == "name" else 157,
        b"\xff",
    )
    target = FakeTree(mutable=True)
    with pytest.raises((UnicodeError, ValueError)):
        RealGuestRecoveryWriter().install(
            target, RELEASE, _bundle(tmp_path / f"undecodable-{field}", archive_bytes)
        )
    assert target.writes == []
