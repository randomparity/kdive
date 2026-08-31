"""Task 2 contract tests for pure module-tree recovery I/O."""

from __future__ import annotations

import hashlib
import inspect
import io
import os
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

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
