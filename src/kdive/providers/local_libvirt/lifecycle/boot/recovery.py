"""Pure, bounded module-tree recovery I/O (ADR-0586)."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import tarfile
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, BinaryIO, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ComponentState,
    ExternalBootActivationBinding,
    PresentComponentState,
)

MAX_ENTRIES = 200_000
MAX_REGULAR_BYTES = 8_589_934_592
MAX_ARCHIVE_BYTES = MAX_REGULAR_BYTES + MAX_ENTRIES * 4096
MAX_OWNER_ID = 2_147_483_647
_ARCHIVE_NAME = "modules.tar"
_DOMAIN = b"kdive-recovery-module-tree-v1\0"
_COPY_CHUNK = 1024 * 1024


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModuleArchiveCapture(_ClosedValue):
    state: Literal["archive"] = "archive"
    manifest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    entry_count: Annotated[int, Field(ge=0, le=MAX_ENTRIES)]
    uncompressed_bytes: Annotated[int, Field(ge=0, le=MAX_REGULAR_BYTES)]
    archive_sha256: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    archive_bytes: Annotated[int, Field(ge=0, le=MAX_ARCHIVE_BYTES)]
    archive_filename: Literal["modules.tar"] = _ARCHIVE_NAME


class AbsentModuleCapture(_ClosedValue):
    state: Literal["absent"] = "absent"


type ModuleCapture = ModuleArchiveCapture | AbsentModuleCapture


class GuestTreeEntry(_ClosedValue):
    path: str
    kind: Literal["directory", "regular", "symlink"]
    mode: Annotated[str, Field(pattern=r"^[0-7]{4}$")]
    uid: Annotated[int, Field(ge=0, le=MAX_OWNER_ID)]
    gid: Annotated[int, Field(ge=0, le=MAX_OWNER_ID)]
    size: Annotated[int, Field(ge=0, le=MAX_REGULAR_BYTES)]
    target: str | None
    xattrs_supported: bool
    xattrs: dict[str, bytes]
    link_count: Annotated[int, Field(ge=1)] = 1


class AuthenticatedGuestTree(Protocol):
    """One-operation, no-follow tree capability created by Task 3."""

    @property
    def binding(self) -> ExternalBootActivationBinding: ...
    @property
    def release(self) -> str: ...
    @property
    def mutable(self) -> bool: ...
    def root_kind(self) -> Literal["absent", "directory", "other"]: ...
    def entries(self) -> Iterator[GuestTreeEntry]: ...
    @contextmanager
    def open_regular(self, path: str, size: int) -> Iterator[BinaryIO]: ...
    def create_directory(self, entry: GuestTreeEntry) -> None: ...
    def create_regular(self, entry: GuestTreeEntry, content: BinaryIO) -> None: ...
    def create_symlink(self, entry: GuestTreeEntry) -> None: ...
    def remove_all(self) -> None: ...


class _ManifestEntry(_ClosedValue):
    path: str
    kind: Literal["directory", "regular", "symlink"]
    mode: Annotated[str, Field(pattern=r"^[0-7]{4}$")]
    uid: Annotated[int, Field(ge=0, le=MAX_OWNER_ID)]
    gid: Annotated[int, Field(ge=0, le=MAX_OWNER_ID)]
    size: Annotated[int, Field(ge=0, le=MAX_REGULAR_BYTES)]
    sha256: str | None
    target: str | None
    xattrs_supported: bool
    xattrs: dict[str, str]


class _SingleUseFile:
    def __init__(self, fd: int, *, expected_size: int, expected_digest: str) -> None:
        _validate_file(fd, expected_size)
        self._fd = os.dup(fd)
        self._expected_size = expected_size
        self._expected_digest = expected_digest
        self._used = False
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            os.close(self._fd)
            self._closed = True

    @contextmanager
    def stream(self) -> Iterator[BinaryIO]:
        if self._used or self._closed:
            raise ValueError("module source capability is not reusable")
        self._used = True
        duplicate = os.dup(self._fd)
        primary: BaseException | None = None
        try:
            with os.fdopen(duplicate, "rb", closefd=True) as source:
                duplicate = -1
                bounded = _VerifiedReader(
                    cast(BinaryIO, source), self._expected_size, self._expected_digest
                )
                yield cast(BinaryIO, bounded)
                bounded.verify_complete()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            cleanup = _close_fd(duplicate) if duplicate >= 0 else None
            try:
                self.close()
            except BaseException as exc:
                cleanup = cleanup or exc
            if cleanup is not None:
                if primary is None:
                    raise cleanup
                primary.add_note(f"module source cleanup failed: {type(cleanup).__name__}")


class KernelBundleSource(_SingleUseFile):
    """Authenticated materialized-module archive, with no path authority."""

    def __init__(
        self,
        fd: int,
        *,
        binding: ExternalBootActivationBinding,
        release: str,
        size: int,
        digest: str,
    ) -> None:
        release = _release(release)
        super().__init__(fd, expected_size=size, expected_digest=digest)
        self.binding = binding
        self.release = release

    def matches(self, tree: AuthenticatedGuestTree, release: str) -> bool:
        return self.binding == tree.binding and self.release == release == tree.release


class RecoveryArchiveSource(_SingleUseFile):
    """Authenticated recovery archive bound to one capture and operation owner."""

    def __init__(
        self,
        directory_fd: int,
        *,
        binding: ExternalBootActivationBinding,
        release: str,
        capture: ModuleArchiveCapture,
    ) -> None:
        release = _release(release)
        _validate_directory(directory_fd)
        self._directory_fd = -1
        self._directory_closed = False
        fd = -1
        try:
            self._directory_fd = os.dup(directory_fd)
            fd = os.open(
                capture.archive_filename,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self._directory_fd,
            )
            super().__init__(
                fd,
                expected_size=capture.archive_bytes,
                expected_digest=capture.archive_sha256,
            )
        except BaseException:
            if self._directory_fd >= 0:
                os.close(self._directory_fd)
            self._directory_closed = True
            raise
        finally:
            if fd >= 0:
                os.close(fd)
        self.binding = binding
        self.release = release
        self.capture = capture

    def close(self) -> None:
        primary: BaseException | None = None
        try:
            super().close()
        except BaseException as exc:
            primary = exc
        try:
            if not self._directory_closed:
                os.close(self._directory_fd)
                self._directory_closed = True
        except BaseException as exc:
            if primary is None:
                raise
            primary.add_note(f"recovery directory close failed: {type(exc).__name__}")
        if primary is not None:
            raise primary

    def matches(
        self,
        tree: AuthenticatedGuestTree,
        release: str,
        capture: ModuleArchiveCapture,
    ) -> bool:
        return (
            self.binding == tree.binding
            and self.release == release == tree.release
            and self.capture == capture
        )


class RecoveryArchiveSink:
    """Authenticated, single-use archive publisher with no exposed host path."""

    def __init__(
        self,
        directory_fd: int,
        *,
        binding: ExternalBootActivationBinding,
        release: str,
    ) -> None:
        release = _release(release)
        _validate_directory(directory_fd)
        self._directory_fd = os.dup(directory_fd)
        self.binding = binding
        self.release = release
        self._used = False
        self._closed = False

    def matches(self, tree: AuthenticatedGuestTree, release: str) -> bool:
        return self.binding == tree.binding and self.release == release == tree.release

    def close(self) -> None:
        if not self._closed:
            os.close(self._directory_fd)
            self._closed = True

    def publish(self, source: BinaryIO) -> tuple[str, int]:
        if self._used or self._closed:
            raise ValueError("recovery archive sink is not reusable")
        self._used = True
        partial = f".{_ARCHIVE_NAME}.partial"
        fd = -1
        created: tuple[int, int] | None = None
        primary: BaseException | None = None
        locked = False
        try:
            fcntl.flock(self._directory_fd, fcntl.LOCK_EX)
            locked = True
            fd = os.open(
                partial,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._directory_fd,
            )
            opened = os.fstat(fd)
            created = (opened.st_dev, opened.st_ino)
            digest, size = _copy_to_fd(source, fd, MAX_ARCHIVE_BYTES)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.rename(
                partial,
                _ARCHIVE_NAME,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            os.fsync(self._directory_fd)
            return digest, size
        except BaseException as exc:
            primary = exc
            cleanup: BaseException | None = None
            if fd >= 0:
                try:
                    os.close(fd)
                except BaseException as exc:
                    cleanup = exc
            if created is not None:
                try:
                    _unlink_if_same(self._directory_fd, partial, created)
                except BaseException as exc:
                    cleanup = cleanup or exc
            if cleanup is not None:
                exc.add_note(f"recovery archive cleanup failed: {type(cleanup).__name__}")
            raise
        finally:
            cleanup: BaseException | None = None
            if locked:
                try:
                    fcntl.flock(self._directory_fd, fcntl.LOCK_UN)
                except BaseException as exc:
                    cleanup = exc
            try:
                self.close()
            except BaseException as exc:
                cleanup = cleanup or exc
            if cleanup is not None:
                if primary is None:
                    raise cleanup
                primary.add_note(f"recovery archive cleanup failed: {type(cleanup).__name__}")


class _VerifiedReader:
    def __init__(self, source: BinaryIO, expected_size: int, expected_digest: str) -> None:
        self._source = source
        self._remaining = expected_size
        self._expected_size = expected_size
        self._expected_digest = expected_digest
        self._digest = hashlib.sha256()
        self._verified = False

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            extra = self._source.read(1)
            if extra:
                raise ValueError("module source exceeds its authenticated size")
            self._verify_digest()
            return b""
        count = self._remaining if size < 0 else min(size, self._remaining)
        data = self._source.read(count)
        if not data:
            raise ValueError("module source ended before its authenticated size")
        self._remaining -= len(data)
        self._digest.update(data)
        return data

    def verify_complete(self) -> None:
        while self.read(_COPY_CHUNK):
            pass

    def _verify_digest(self) -> None:
        if self._verified:
            return
        if "sha256:" + self._digest.hexdigest() != self._expected_digest:
            raise ValueError("module source digest does not match authenticated metadata")
        self._verified = True


class GuestRecoveryWriter(Protocol):
    def capture(
        self, tree: AuthenticatedGuestTree, release: str, sink: RecoveryArchiveSink
    ) -> ModuleCapture: ...
    def observe(self, tree: AuthenticatedGuestTree, release: str) -> ComponentState: ...
    def install(
        self, tree: AuthenticatedGuestTree, release: str, source: KernelBundleSource
    ) -> str: ...
    def restore(
        self,
        tree: AuthenticatedGuestTree,
        release: str,
        capture: ModuleCapture,
        source: RecoveryArchiveSource,
    ) -> str: ...


class RealGuestRecoveryWriter:
    """Validate and copy one module tree without lifecycle or publication authority."""

    def capture(
        self, tree: AuthenticatedGuestTree, release: str, sink: RecoveryArchiveSink
    ) -> ModuleCapture:
        _match_tree(tree, release, mutable=False)
        if not sink.matches(tree, release):
            sink.close()
            raise ValueError("recovery archive sink owner does not match guest tree")
        if tree.root_kind() == "absent":
            sink.close()
            return AbsentModuleCapture()
        entries = _validated_entries(tree)
        with tempfile.TemporaryFile() as archive:
            manifests = _write_archive(tree, entries, archive)
            _, manifest, count, size = _manifest(manifests)
            archive.seek(0)
            archive_digest, archive_size = sink.publish(archive)
        return ModuleArchiveCapture(
            manifest=manifest,
            entry_count=count,
            uncompressed_bytes=size,
            archive_sha256=archive_digest,
            archive_bytes=archive_size,
        )

    def observe(self, tree: AuthenticatedGuestTree, release: str) -> ComponentState:
        _match_tree(tree, release, mutable=False)
        if tree.root_kind() == "absent":
            return AbsentComponentState()
        manifests = _hash_entries(tree, _validated_entries(tree))
        return PresentComponentState(manifest=_manifest(manifests)[1])

    def install(
        self, tree: AuthenticatedGuestTree, release: str, source: KernelBundleSource
    ) -> str:
        _match_tree(tree, release, mutable=True)
        if not source.matches(tree, release):
            source.close()
            raise ValueError("kernel bundle source owner or release does not match guest tree")
        with source.stream() as stream, tempfile.TemporaryFile() as staged:
            _copy_stream(stream, staged, MAX_ARCHIVE_BYTES)
            stream.read(1)
            staged.seek(0)
            entries = _validate_archive(staged)
            staged.seek(0)
            _populate_from_archive(tree, staged, entries)
        return _manifest(entries)[1]

    def restore(
        self,
        tree: AuthenticatedGuestTree,
        release: str,
        capture: ModuleCapture,
        source: RecoveryArchiveSource,
    ) -> str:
        _match_tree(tree, release, mutable=True)
        if isinstance(capture, AbsentModuleCapture):
            source.close()
            tree.remove_all()
            return capture.state
        if not source.matches(tree, release, capture):
            source.close()
            raise ValueError("recovery archive source identity does not match guest tree")
        with source.stream() as stream, tempfile.TemporaryFile() as staged:
            _copy_stream(stream, staged, MAX_ARCHIVE_BYTES)
            stream.read(1)
            staged.seek(0)
            entries = _validate_archive(staged)
            if (_manifest(entries)[1], len(entries), _regular_size(entries)) != (
                capture.manifest,
                capture.entry_count,
                capture.uncompressed_bytes,
            ):
                raise ValueError("recovery archive manifest does not match capture")
            staged.seek(0)
            _populate_from_archive(tree, staged, entries)
        return capture.manifest


def _validated_entries(tree: AuthenticatedGuestTree) -> list[GuestTreeEntry]:
    if tree.root_kind() != "directory":
        raise ValueError("authenticated module-tree root is not a directory")
    result: list[GuestTreeEntry] = []
    seen: set[str] = set()
    total = 0
    for entry in tree.entries():
        path = _path(entry.path)
        if path in seen:
            raise ValueError("module tree contains duplicate paths")
        seen.add(path)
        if entry.kind == "regular":
            if entry.link_count != 1:
                raise ValueError("hard-linked module entries are forbidden")
            total += entry.size
            if total > MAX_REGULAR_BYTES:
                raise ValueError("module tree exceeds 8589934592 regular bytes")
        elif entry.size != 0:
            raise ValueError("non-regular module entry has content bytes")
        if entry.kind == "symlink":
            _text(cast(str, entry.target), "symlink target")
        elif entry.target is not None:
            raise ValueError("non-symlink module entry has a target")
        if not entry.xattrs_supported and entry.xattrs:
            raise ValueError("module tree has xattrs while support is false")
        _xattrs(entry.xattrs)
        result.append(entry.model_copy(update={"path": path}))
        if len(result) > MAX_ENTRIES:
            raise ValueError("module tree exceeds 200000 entries")
    return sorted(result, key=lambda item: item.path.encode())


def _hash_entries(
    tree: AuthenticatedGuestTree, entries: list[GuestTreeEntry]
) -> list[_ManifestEntry]:
    return [_manifest_entry(tree, entry) for entry in entries]


def _manifest_entry(tree: AuthenticatedGuestTree, entry: GuestTreeEntry) -> _ManifestEntry:
    digest: str | None = None
    if entry.kind == "regular":
        with tree.open_regular(entry.path, entry.size) as content:
            digest, size = _hash_stream(content, entry.size)
        if size != entry.size:
            raise ValueError("module entry changed during no-follow read")
    return _ManifestEntry(
        path=entry.path,
        kind=entry.kind,
        mode=entry.mode,
        uid=entry.uid,
        gid=entry.gid,
        size=entry.size,
        sha256=digest,
        target=entry.target,
        xattrs_supported=entry.xattrs_supported,
        xattrs={
            name: base64.b64encode(value).decode("ascii").rstrip("=")
            for name, value in sorted(entry.xattrs.items(), key=lambda item: item[0].encode())
        },
    )


def _write_archive(
    tree: AuthenticatedGuestTree,
    entries: list[GuestTreeEntry],
    destination: BinaryIO,
) -> list[_ManifestEntry]:
    manifests: list[_ManifestEntry] = []
    with tarfile.open(fileobj=destination, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            member = _tar_info(entry)
            if entry.kind == "regular":
                with (
                    tree.open_regular(entry.path, entry.size) as source,
                    tempfile.SpooledTemporaryFile(max_size=_COPY_CHUNK) as content,
                ):
                    digest, size = _copy_and_hash(source, cast(BinaryIO, content), entry.size)
                    if size != entry.size:
                        raise ValueError("module entry changed during no-follow read")
                    content.seek(0)
                    archive.addfile(member, content)
                manifests.append(_manifest_entry_with_digest(entry, digest))
            else:
                archive.addfile(member)
                manifests.append(_manifest_entry_with_digest(entry, None))
    return manifests


def _validate_archive(source: BinaryIO) -> list[_ManifestEntry]:
    entries: list[_ManifestEntry] = []
    seen: set[str] = set()
    total = 0
    with tarfile.open(fileobj=source, mode="r|") as archive:
        for member in archive:
            path = _path(member.name)
            if path in seen:
                raise ValueError("module archive contains duplicate paths")
            seen.add(path)
            kind = _member_kind(member)
            if kind == "regular":
                total += member.size
                if total > MAX_REGULAR_BYTES:
                    raise ValueError("module archive exceeds 8589934592 regular bytes")
                content = cast(BinaryIO, archive.extractfile(member))
                digest, size = _hash_stream(content, member.size)
                if size != member.size:
                    raise ValueError("module archive regular entry ended early")
            else:
                digest, size = None, 0
            entries.append(_manifest_from_member(member, path, kind, digest, size))
            if len(entries) > MAX_ENTRIES:
                raise ValueError("module archive exceeds 200000 entries")
    _manifest(entries)
    return entries


def _populate_from_archive(
    tree: AuthenticatedGuestTree,
    source: BinaryIO,
    expected: list[_ManifestEntry],
) -> None:
    expected_by_path = {entry.path: entry for entry in expected}
    with tarfile.open(fileobj=source, mode="r|") as archive:
        for member in archive:
            manifest = expected_by_path[_path(member.name)]
            entry = _guest_entry(manifest)
            if entry.kind == "directory":
                tree.create_directory(entry)
            elif entry.kind == "symlink":
                tree.create_symlink(entry)
            else:
                tree.create_regular(entry, cast(BinaryIO, archive.extractfile(member)))


def _manifest(entries: list[_ManifestEntry]) -> tuple[bytes, str, int, int]:
    ordered = sorted(entries, key=lambda item: item.path.encode())
    data = json.dumps(
        {
            "entries": [entry.model_dump(mode="json") for entry in ordered],
            "schema": "recovery-module-tree-v1",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return (
        data,
        "sha256:" + hashlib.sha256(_DOMAIN + data).hexdigest(),
        len(ordered),
        _regular_size(ordered),
    )


def _tar_info(entry: GuestTreeEntry) -> tarfile.TarInfo:
    member = tarfile.TarInfo(entry.path)
    member.mode, member.uid, member.gid = int(entry.mode, 8), entry.uid, entry.gid
    member.mtime = 0
    member.size = entry.size
    member.pax_headers = {
        "KDIVE.xattrs-supported": "1" if entry.xattrs_supported else "0",
        **dict(
            sorted(
                {
                    f"KDIVE.xattr.{name}": base64.b64encode(value).decode().rstrip("=")
                    for name, value in entry.xattrs.items()
                }.items(),
                key=lambda item: item[0].removeprefix("KDIVE.xattr.").encode(),
            )
        ),
    }
    if entry.kind == "directory":
        member.type, member.size = tarfile.DIRTYPE, 0
    elif entry.kind == "symlink":
        member.type, member.size, member.linkname = tarfile.SYMTYPE, 0, cast(str, entry.target)
    return member


def _manifest_from_member(
    member: tarfile.TarInfo,
    path: str,
    kind: Literal["directory", "regular", "symlink"],
    digest: str | None,
    size: int,
) -> _ManifestEntry:
    _validate_member_shape(member, kind)
    supported = member.pax_headers.get("KDIVE.xattrs-supported")
    if supported not in {"0", "1"}:
        raise ValueError("module archive lacks canonical xattr support metadata")
    allowed = {"KDIVE.xattrs-supported", "path", "linkpath"}
    attrs = {
        key.removeprefix("KDIVE.xattr."): value
        for key, value in member.pax_headers.items()
        if key.startswith("KDIVE.xattr.")
    }
    if any(key not in allowed and not key.startswith("KDIVE.xattr.") for key in member.pax_headers):
        raise ValueError("module archive contains unknown metadata")
    if supported == "0" and attrs:
        raise ValueError("module archive has xattrs while support is false")
    for name, value in attrs.items():
        _text(name, "xattr name")
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
        if "=" in value or base64.b64encode(decoded).decode().rstrip("=") != value:
            raise ValueError("module archive xattr is not canonical base64")
    return _ManifestEntry(
        path=path,
        kind=kind,
        mode=f"{member.mode:04o}",
        uid=member.uid,
        gid=member.gid,
        size=size,
        sha256=digest,
        target=_text(member.linkname, "symlink target") if kind == "symlink" else None,
        xattrs_supported=supported == "1",
        xattrs=dict(sorted(attrs.items(), key=lambda item: item[0].encode())),
    )


def _manifest_entry_with_digest(entry: GuestTreeEntry, digest: str | None) -> _ManifestEntry:
    return _ManifestEntry(
        path=entry.path,
        kind=entry.kind,
        mode=entry.mode,
        uid=entry.uid,
        gid=entry.gid,
        size=entry.size,
        sha256=digest,
        target=entry.target,
        xattrs_supported=entry.xattrs_supported,
        xattrs={
            name: base64.b64encode(value).decode().rstrip("=")
            for name, value in sorted(entry.xattrs.items(), key=lambda item: item[0].encode())
        },
    )


def _guest_entry(entry: _ManifestEntry) -> GuestTreeEntry:
    return GuestTreeEntry(
        path=entry.path,
        kind=entry.kind,
        mode=entry.mode,
        uid=entry.uid,
        gid=entry.gid,
        size=entry.size,
        target=entry.target,
        xattrs_supported=entry.xattrs_supported,
        xattrs={
            name: base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
            for name, value in entry.xattrs.items()
        },
    )


def _member_kind(member: tarfile.TarInfo) -> Literal["directory", "regular", "symlink"]:
    if member.islnk() or not (member.isdir() or member.isfile() or member.issym()):
        raise ValueError("module archive contains forbidden topology")
    return "directory" if member.isdir() else "symlink" if member.issym() else "regular"


def _validate_member_shape(
    member: tarfile.TarInfo, kind: Literal["directory", "regular", "symlink"]
) -> None:
    if member.mode < 0 or member.mode > 0o7777:
        raise ValueError("module archive mode is outside the canonical range")
    if not 0 <= member.uid <= MAX_OWNER_ID or not 0 <= member.gid <= MAX_OWNER_ID:
        raise ValueError("module archive owner id is outside the canonical range")
    if kind == "regular" and (member.size < 0 or member.linkname):
        raise ValueError("module archive regular metadata is not canonical")
    if kind == "directory" and (member.size != 0 or member.linkname):
        raise ValueError("module archive directory metadata is not canonical")
    if kind == "symlink" and (member.size != 0 or not member.linkname):
        raise ValueError("module archive symlink metadata is not canonical")


def _match_tree(tree: AuthenticatedGuestTree, release: str, *, mutable: bool) -> None:
    release = _release(release)
    if tree.release != release:
        raise ValueError("guest-tree release does not match the requested release")
    if tree.mutable != mutable:
        raise ValueError("guest-tree access mode does not match the operation")


def _path(value: str) -> str:
    value = _text(value, "path")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("module-tree path is not canonical relative text")
    return value


def _release(value: str) -> str:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-"
    if (
        not value
        or len(value) > 128
        or value[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        or any(character not in allowed for character in value)
    ):
        raise ValueError("kernel release is not canonical")
    return value


def _text(value: str, field: str) -> str:
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ValueError(f"module-tree {field} is not UTF-8") from exc
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"module-tree {field} is not NFC")
    return value


def _xattrs(values: dict[str, bytes]) -> None:
    for name in values:
        _text(name, "xattr name")


def _validate_directory(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("recovery archive root is not a directory")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("recovery archive root ownership or mode is invalid")


def _validate_file(fd: int, expected_size: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("module source is not a regular file")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("module source ownership or mode is invalid")
    if info.st_size != expected_size or info.st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("module source size does not match authenticated metadata")


def _copy_to_fd(source: BinaryIO, fd: int, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(_COPY_CHUNK):
        size += len(chunk)
        if size > limit:
            raise ValueError("module archive exceeds its byte reservation")
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("short write publishing module archive")
            view = view[written:]
    return "sha256:" + digest.hexdigest(), size


def _copy_stream(source: BinaryIO, destination: BinaryIO, limit: int) -> None:
    size = 0
    while chunk := source.read(_COPY_CHUNK):
        size += len(chunk)
        if size > limit:
            raise ValueError("module source exceeds its byte reservation")
        destination.write(chunk)


def _hash_stream(source: BinaryIO, expected: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(min(_COPY_CHUNK, expected - size + 1)):
        size += len(chunk)
        if size > expected:
            raise ValueError("module content exceeds its authenticated size")
        digest.update(chunk)
    return "sha256:" + digest.hexdigest(), size


def _copy_and_hash(source: BinaryIO, destination: BinaryIO, expected: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(min(_COPY_CHUNK, expected - size + 1)):
        size += len(chunk)
        if size > expected:
            raise ValueError("module content exceeds its authenticated size")
        digest.update(chunk)
        destination.write(chunk)
    return "sha256:" + digest.hexdigest(), size


def _regular_size(entries: list[_ManifestEntry]) -> int:
    return sum(entry.size for entry in entries if entry.kind == "regular")


def _close_fd(fd: int) -> BaseException | None:
    try:
        os.close(fd)
    except BaseException as exc:
        return exc
    return None


def _unlink_if_same(directory_fd: int, name: str, expected: tuple[int, int]) -> None:
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != expected:
        return
    os.unlink(name, dir_fd=directory_fd)
