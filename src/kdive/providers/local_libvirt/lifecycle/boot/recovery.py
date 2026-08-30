"""Bounded module-tree capture and restoration for local external boot (ADR-0586)."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, BinaryIO, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ComponentState,
    PresentComponentState,
)

MAX_ENTRIES = 200_000
MAX_REGULAR_BYTES = 8_589_934_592
MAX_ARCHIVE_BYTES = MAX_REGULAR_BYTES
_DOMAIN = b"kdive-recovery-module-tree-v1\0"
_ARCHIVE_NAME = "modules.tar"
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModuleArchiveCapture(_ClosedValue):
    state: Literal["archive"] = "archive"
    manifest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    entry_count: Annotated[int, Field(ge=0, le=MAX_ENTRIES)]
    uncompressed_bytes: Annotated[int, Field(ge=0, le=MAX_REGULAR_BYTES)]
    archive_sha256: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    archive_filename: Literal["modules.tar"] = _ARCHIVE_NAME


class AbsentModuleCapture(_ClosedValue):
    state: Literal["absent"] = "absent"


type ModuleCapture = ModuleArchiveCapture | AbsentModuleCapture


class RecoveryArchiveSink:
    """Single-use archive publisher bound to an authenticated recovery directory."""

    def __init__(self, directory_fd: int) -> None:
        _validate_directory(directory_fd, os.geteuid())
        self._directory_fd = os.dup(directory_fd)
        self._used = False
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            os.close(self._directory_fd)
            self._closed = True

    def publish(self, chunks: Iterable[bytes]) -> None:
        if self._used:
            raise ValueError("recovery archive sink was already used")
        self._used = True
        partial = f".{_ARCHIVE_NAME}.partial"
        fd = -1
        try:
            fd = os.open(
                partial,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._directory_fd,
            )
            total = 0
            for chunk in chunks:
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError("recovery archive exceeds its byte reservation")
                _write_all(fd, chunk)
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
        except Exception:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(partial, dir_fd=self._directory_fd)
            raise
        finally:
            self.close()


class RecoveryArchiveSource:
    """Single-use bounded reader bound to an authenticated recovery directory."""

    def __init__(self, directory_fd: int, *, service_uid: int | None = None) -> None:
        self._service_uid = os.geteuid() if service_uid is None else service_uid
        _validate_directory(directory_fd, self._service_uid)
        self._directory_fd = os.dup(directory_fd)
        self._used = False
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            os.close(self._directory_fd)
            self._closed = True

    @contextmanager
    def open(self, capture: ModuleArchiveCapture) -> Iterator[BinaryIO]:
        if self._used:
            raise ValueError("recovery archive source was already used")
        self._used = True
        fd = -1
        try:
            fd = os.open(
                capture.archive_filename,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self._directory_fd,
            )
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("recovery archive is not a regular file")
            if info.st_uid != self._service_uid or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("recovery archive ownership or mode is invalid")
            if info.st_size > MAX_ARCHIVE_BYTES:
                raise ValueError("recovery archive exceeds its byte reservation")
            with os.fdopen(fd, "rb", closefd=True) as stream:
                fd = -1
                yield cast(BinaryIO, stream)
        finally:
            if fd >= 0:
                os.close(fd)
            self.close()


class _Entry(_ClosedValue):
    path: str
    kind: Literal["directory", "regular", "symlink"]
    mode: str
    uid: int
    gid: int
    size: int
    sha256: str | None
    target: str | None
    xattrs_supported: bool
    xattrs: dict[str, str]


class _Guest(Protocol):  # pragma: no cover - libguestfs binding surface
    def exists(self, path: str) -> int: ...
    def ls(self, path: str) -> list[str]: ...
    def lstatns(self, path: str) -> dict[str, int]: ...
    def readlink(self, path: str) -> str: ...
    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]: ...
    def download(self, remote: str, local: str) -> None: ...
    def upload(self, local: str, remote: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def mkdir_p(self, path: str) -> None: ...
    def ln_s(self, target: str, linkname: str) -> None: ...
    def lchown(self, uid: int, gid: int, path: str) -> None: ...
    def chmod(self, mode: int, path: str) -> None: ...
    def lsetxattr(self, xattr: str, value: bytes, vallen: int, path: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...
    def mv(self, source: str, target: str) -> None: ...
    def sync(self) -> None: ...
    def shutdown(self) -> None: ...
    def close(self) -> None: ...


class GuestRecoveryWriter(Protocol):
    def capture(self, overlay: str, release: str, sink: RecoveryArchiveSink) -> ModuleCapture: ...
    def observe(self, overlay: str, release: str) -> ComponentState: ...
    def install(self, overlay: str, release: str, source: Path) -> str: ...
    def restore(
        self,
        overlay: str,
        release: str,
        capture: ModuleCapture,
        source: RecoveryArchiveSource,
    ) -> str: ...


class RealGuestRecoveryWriter:
    """Use one inactive read-write libguestfs mount for each operation."""

    def __init__(self, opener: Callable[[str], _Guest] | None = None) -> None:
        self._opener = opener or _open_guest

    def capture(self, overlay: str, release: str, sink: RecoveryArchiveSink) -> ModuleCapture:
        guest = self._opener(overlay)
        try:
            root = _release_root(release)
            if not guest.exists(root):
                sink.close()
                return AbsentModuleCapture()
            entries, contents = _walk(guest, root)
            manifest, digest, count, size = _manifest(entries)
            archive = _archive(entries, contents)
            sink.publish((archive,))
            return ModuleArchiveCapture(
                manifest=digest,
                entry_count=count,
                uncompressed_bytes=size,
                archive_sha256=_sha256(archive),
            )
        finally:
            _close_guest(guest)

    def observe(self, overlay: str, release: str) -> ComponentState:
        guest = self._opener(overlay)
        try:
            root = _release_root(release)
            if not guest.exists(root):
                return AbsentComponentState()
            entries, _ = _walk(guest, root)
            return PresentComponentState(manifest=_manifest(entries)[1])
        finally:
            _close_guest(guest)

    def install(self, overlay: str, release: str, source: Path) -> str:
        with source.open("rb") as stream:
            return self._replace(overlay, release, stream, expected=None)

    def restore(
        self,
        overlay: str,
        release: str,
        capture: ModuleCapture,
        source: RecoveryArchiveSource,
    ) -> str:
        if isinstance(capture, AbsentModuleCapture):
            source.close()
            guest = self._opener(overlay)
            try:
                guest.rm_rf(_release_root(release))
                guest.sync()
                return capture.state
            finally:
                _close_guest(guest)
        with source.open(capture) as stream:
            return self._replace(overlay, release, stream, expected=capture)

    def _replace(
        self,
        overlay: str,
        release: str,
        stream: BinaryIO,
        *,
        expected: ModuleArchiveCapture | None,
    ) -> str:
        archive = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(archive) > MAX_ARCHIVE_BYTES:
            raise ValueError("recovery archive exceeds its byte reservation")
        if expected is not None and _sha256(archive) != expected.archive_sha256:
            raise ValueError("recovery archive digest does not match capture")
        entries, contents = _read_archive(archive)
        _, manifest, count, size = _manifest(entries)
        if expected is not None and (manifest, count, size) != (
            expected.manifest,
            expected.entry_count,
            expected.uncompressed_bytes,
        ):
            raise ValueError("recovery archive manifest does not match capture")
        self._stage(overlay, release, entries, contents, manifest)
        return manifest

    def _stage(
        self,
        overlay: str,
        release: str,
        entries: list[_Entry],
        contents: dict[str, bytes],
        expected: str,
    ) -> None:
        guest = self._opener(overlay)
        root = _release_root(release)
        partial = f"{root}.kdive-partial"
        previous = f"{root}.kdive-previous"
        try:
            guest.rm_rf(partial)
            if guest.exists(previous):
                raise ValueError("owned recovery partial requires classification")
            guest.mkdir_p(partial)
            _populate(guest, partial, entries, contents)
            observed, _ = _walk(guest, partial)
            if _manifest(observed)[1] != expected:
                raise ValueError("staged recovery module manifest does not match source")
            if guest.exists(root):
                guest.mv(root, previous)
            try:
                guest.mv(partial, root)
            except Exception:
                if not guest.exists(root) and guest.exists(previous):
                    guest.mv(previous, root)
                raise
            guest.rm_rf(previous)
            guest.sync()
        except Exception:
            with contextlib.suppress(Exception):
                guest.rm_rf(partial)
            raise
        finally:
            _close_guest(guest)


def _manifest(entries: Iterable[_Entry]) -> tuple[bytes, str, int, int]:
    ordered = sorted(entries, key=lambda item: item.path.encode("utf-8"))
    if len(ordered) > MAX_ENTRIES:
        raise ValueError("recovery module tree exceeds 200000 entries")
    if len({entry.path for entry in ordered}) != len(ordered):
        raise ValueError("recovery module tree contains duplicate paths")
    size = sum(entry.size for entry in ordered if entry.kind == "regular")
    if size > MAX_REGULAR_BYTES:
        raise ValueError("recovery module tree exceeds 8589934592 regular bytes")
    data = json.dumps(
        {
            "entries": [entry.model_dump(mode="json") for entry in ordered],
            "schema": "recovery-module-tree-v1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return data, "sha256:" + hashlib.sha256(_DOMAIN + data).hexdigest(), len(ordered), size


def _walk(guest: _Guest, root: str) -> tuple[list[_Entry], dict[str, bytes]]:
    entries: list[_Entry] = []
    contents: dict[str, bytes] = {}
    xattrs_supported: bool | None = None
    regular_bytes = 0

    def visit(directory: str, relative: str = "") -> None:
        nonlocal regular_bytes, xattrs_supported
        names = sorted(guest.ls(directory), key=lambda value: _text(value, "entry name").encode())
        for name in names:
            canonical_name = _component(name)
            path = f"{directory}/{canonical_name}"
            relative_path = f"{relative}/{canonical_name}".lstrip("/")
            info = guest.lstatns(path)
            attrs, supported = _read_xattrs(guest, path, xattrs_supported)
            xattrs_supported = supported if xattrs_supported is None else xattrs_supported
            common = {
                "path": relative_path,
                "mode": f"{stat.S_IMODE(info['st_mode']):04o}",
                "uid": info["st_uid"],
                "gid": info["st_gid"],
                "xattrs_supported": supported,
                "xattrs": attrs,
            }
            mode = info["st_mode"]
            if stat.S_ISDIR(mode):
                entries.append(_Entry(kind="directory", size=0, sha256=None, target=None, **common))
                visit(path, relative_path)
            elif stat.S_ISREG(mode):
                if info.get("st_nlink", 1) != 1:
                    raise ValueError("hard-linked recovery entries are forbidden")
                regular_bytes += info["st_size"]
                if regular_bytes > MAX_REGULAR_BYTES:
                    raise ValueError("recovery module tree exceeds 8589934592 regular bytes")
                content = _download(guest, path)
                if len(content) != info["st_size"]:
                    raise ValueError("recovery entry changed while it was read")
                contents[relative_path] = content
                entries.append(
                    _Entry(
                        kind="regular",
                        size=len(content),
                        sha256=_sha256(content),
                        target=None,
                        **common,
                    )
                )
            elif stat.S_ISLNK(mode):
                target = _text(guest.readlink(path), "symlink target")
                entries.append(_Entry(kind="symlink", size=0, sha256=None, target=target, **common))
            else:
                raise ValueError("special recovery entries are forbidden")
            if len(entries) > MAX_ENTRIES:
                raise ValueError("recovery module tree exceeds 200000 entries")

    visit(root)
    _manifest(entries)
    return entries, contents


def _read_xattrs(guest: _Guest, path: str, established: bool | None) -> tuple[dict[str, str], bool]:
    if established is False:
        return {}, False
    try:
        source = guest.lgetxattrs(path)
    except NotImplementedError:
        if established:
            raise ValueError(
                "recovery xattrs became unreadable after support was established"
            ) from None
        return {}, False
    result: dict[str, str] = {}
    for item in source:
        name = _text(cast(str, item["attrname"]), "xattr name")
        raw = item["attrval"]
        value = raw if isinstance(raw, bytes) else raw.encode("latin1")
        result[name] = base64.b64encode(value).decode("ascii").rstrip("=")
    return dict(sorted(result.items(), key=lambda item: item[0].encode())), True


def _archive(entries: list[_Entry], contents: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for entry in sorted(entries, key=lambda item: item.path.encode()):
            member = tarfile.TarInfo(entry.path)
            member.mode, member.uid, member.gid = int(entry.mode, 8), entry.uid, entry.gid
            member.mtime = 0
            member.pax_headers = {
                "KDIVE.xattrs-supported": "1" if entry.xattrs_supported else "0",
                **{f"KDIVE.xattr.{name}": value for name, value in entry.xattrs.items()},
            }
            if entry.kind == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif entry.kind == "symlink":
                member.type, member.linkname = tarfile.SYMTYPE, cast(str, entry.target)
                archive.addfile(member)
            else:
                payload = contents[entry.path]
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def _read_archive(data: bytes) -> tuple[list[_Entry], dict[str, bytes]]:
    entries: list[_Entry] = []
    contents: dict[str, bytes] = {}
    regular_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            if len(entries) >= MAX_ENTRIES:
                raise ValueError("recovery module tree exceeds 200000 entries")
            path = _relative_path(member.name)
            if member.islnk() or not (member.isdir() or member.isfile() or member.issym()):
                raise ValueError("recovery archive contains forbidden topology")
            if member.isfile():
                regular_bytes += member.size
                if regular_bytes > MAX_REGULAR_BYTES:
                    raise ValueError("recovery module tree exceeds 8589934592 regular bytes")
            content = cast(BinaryIO, archive.extractfile(member)).read() if member.isfile() else b""
            kind: Literal["directory", "regular", "symlink"]
            kind = "directory" if member.isdir() else "symlink" if member.issym() else "regular"
            attrs = {
                key.removeprefix("KDIVE.xattr."): value
                for key, value in member.pax_headers.items()
                if key.startswith("KDIVE.xattr.")
            }
            supported = member.pax_headers.get("KDIVE.xattrs-supported")
            if supported not in {"0", "1"}:
                raise ValueError("recovery archive lacks canonical xattr metadata")
            entry = _Entry(
                path=path,
                kind=kind,
                mode=f"{member.mode:04o}",
                uid=member.uid,
                gid=member.gid,
                size=len(content),
                sha256=_sha256(content) if member.isfile() else None,
                target=_text(member.linkname, "symlink target") if member.issym() else None,
                xattrs_supported=supported == "1",
                xattrs=dict(sorted(attrs.items(), key=lambda item: item[0].encode())),
            )
            entries.append(entry)
            if member.isfile():
                contents[path] = content
    _manifest(entries)
    return entries, contents


def _populate(guest: _Guest, root: str, entries: list[_Entry], contents: dict[str, bytes]) -> None:
    ordered = sorted(entries, key=lambda item: (item.path.count("/"), item.path.encode()))
    for entry in ordered:
        destination = f"{root}/{entry.path}"
        if entry.kind == "directory":
            guest.mkdir(destination)
        elif entry.kind == "symlink":
            guest.ln_s(cast(str, entry.target), destination)
        else:
            with tempfile.NamedTemporaryFile() as handle:
                handle.write(contents[entry.path])
                handle.flush()
                guest.upload(handle.name, destination)
        if entry.kind != "directory":
            _apply_metadata(guest, destination, entry)
    for entry in reversed(ordered):
        if entry.kind == "directory":
            _apply_metadata(guest, f"{root}/{entry.path}", entry)


def _apply_metadata(guest: _Guest, destination: str, entry: _Entry) -> None:
    guest.lchown(entry.uid, entry.gid, destination)
    if entry.kind != "symlink":
        guest.chmod(int(entry.mode, 8), destination)
    if entry.xattrs_supported:
        for name, encoded in entry.xattrs.items():
            value = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
            guest.lsetxattr(name, value, len(value), destination)


def _download(guest: _Guest, path: str) -> bytes:
    with tempfile.NamedTemporaryFile() as handle:
        guest.download(path, handle.name)
        return Path(handle.name).read_bytes()


def _release_root(release: str) -> str:
    if not _RELEASE.fullmatch(release):
        raise ValueError("kernel release is not canonical")
    return f"/lib/modules/{release}"


def _component(value: str) -> str:
    value = _text(value, "entry name")
    if value in {"", ".", ".."} or "/" in value:
        raise ValueError("recovery entry name is not canonical")
    return value


def _relative_path(value: str) -> str:
    value = _text(value, "archive path")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("recovery archive path is not canonical")
    return value


def _text(value: str, field: str) -> str:
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ValueError(f"recovery {field} is not UTF-8") from exc
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"recovery {field} is not NFC")
    return value


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_all(fd: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("short write publishing recovery archive")
        view = view[written:]


def _validate_directory(directory_fd: int, service_uid: int) -> None:
    info = os.fstat(directory_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("recovery archive capability requires a directory")
    if info.st_uid != service_uid or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("recovery archive directory ownership or mode is invalid")


def _open_guest(overlay: str) -> _Guest:  # pragma: no cover - live_vm
    import guestfs  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided

    guest = cast(_Guest, guestfs.GuestFS(python_return_dict=True))
    try:
        guest.add_drive_opts(overlay, format="qcow2", readonly=False)  # ty: ignore[unresolved-attribute]
        guest.launch()  # ty: ignore[unresolved-attribute]
        roots = guest.inspect_os()  # ty: ignore[unresolved-attribute]
        if not roots:
            raise ValueError("System overlay has no inspectable root")
        guest.mount(roots[0], "/")  # ty: ignore[unresolved-attribute]
        return guest
    except Exception:
        _close_guest(guest)
        raise


def _close_guest(guest: _Guest) -> None:
    with contextlib.suppress(Exception):
        guest.shutdown()
    with contextlib.suppress(Exception):
        guest.close()
