"""Attempt-scoped storage for the confined remote-module appliance (ADRs 0585, 0588)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

import libvirt
from defusedxml.common import DefusedXmlException

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    ModuleVolumeOwner,
    parse_module_volume_name,
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.xml_bounds import parse_libvirt_xml

MAX_ENTRIES = 200_000
MAX_CONTENT_BYTES = 8 * 1024**3
SCRATCH_CAPACITY_BYTES = 10 * 1024**3
_MIN_SOURCE_CAPACITY_BYTES = 64 * 1024**2
_SOURCE_BLOCK_BYTES = 4096
_SOURCE_FIXED_OVERHEAD_BYTES = 16 * 1024**2
_SOURCE_HEADROOM_DIVISOR = 8
_SOURCE_ROOT_INODES = 16


@dataclass(frozen=True, slots=True)
class ModuleTreeEntry:
    path: str
    mode: int
    content: bytes | None = None
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class SourceFilesystemEvidence:
    operation: bytes
    manifest: str
    entry_count: int
    content_bytes: int


@dataclass(frozen=True, slots=True)
class BuiltSourceImage:
    path: Path
    capacity_bytes: int
    evidence: SourceFilesystemEvidence


class FilesystemImageWriter(Protocol):
    def build(self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]) -> BuiltSourceImage: ...

    def inspect(self, path: Path) -> SourceFilesystemEvidence: ...


class Ext4SourceFilesystemWriter:
    """Build and reopen the appliance's closed ext4 source layout with e2fsprogs."""

    def __init__(self, work_dir: Path) -> None:
        self._work_dir = work_dir

    def build(self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]) -> BuiltSourceImage:
        _validate_entries(entries)
        staging = Path(tempfile.mkdtemp(prefix="kdive-module-source-", dir=self._work_dir))
        descriptor, raw_image = tempfile.mkstemp(
            prefix="kdive-module-source-", suffix=".ext4", dir=self._work_dir
        )
        os.close(descriptor)
        image = Path(raw_image)
        succeeded = False
        try:
            (staging / "operation-v1.json").write_bytes(operation)
            modules = staging / "modules"
            modules.mkdir(mode=0o755)
            ordered = sorted(
                entries,
                key=lambda value: (len(PurePosixPath(value.path).parts), value.path),
            )
            for entry in ordered:
                destination = modules.joinpath(*PurePosixPath(entry.path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                kind = stat.S_IFMT(entry.mode)
                if kind == stat.S_IFDIR:
                    destination.mkdir(exist_ok=True)
                    destination.chmod(0o755)
                elif kind == stat.S_IFREG:
                    assert entry.content is not None
                    destination.write_bytes(entry.content)
                    destination.chmod(0o755 if entry.mode & 0o111 else 0o644)
                else:
                    assert entry.link_target is not None
                    destination.symlink_to(entry.link_target)
            content_bytes = sum(len(entry.content or b"") for entry in entries)
            capacity = source_image_capacity_bytes(content_bytes, len(entries))
            subprocess.run(  # noqa: S603 - fixed executable and closed argument vector
                [
                    "mkfs.ext4",  # noqa: S607
                    "-q",
                    "-F",
                    "-b",
                    str(_SOURCE_BLOCK_BYTES),
                    "-N",
                    str(source_image_inode_count(len(entries))),
                    "-d",
                    str(staging),
                    str(image),
                    str(capacity // _SOURCE_BLOCK_BYTES),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            evidence = self.inspect(image)
            size = image.stat().st_size
            succeeded = True
        except (OSError, subprocess.SubprocessError, CategorizedError, ValueError) as exc:
            raise CategorizedError(
                "failed to build remote module ext4 source image",
                category=ErrorCategory.BUILD_FAILURE,
                details={"tool": "mkfs.ext4"},
            ) from exc
        finally:
            try:
                shutil.rmtree(staging)
            except BaseException:
                image.unlink(missing_ok=True)
                raise
            finally:
                if not succeeded:
                    image.unlink(missing_ok=True)
        return BuiltSourceImage(image, size, evidence)

    def extract(self, image: Path, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(  # noqa: S603 - fixed executable and closed argument vector
                ["debugfs", "-R", f"rdump / {destination}", str(image)],  # noqa: S607
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CategorizedError(
                "failed to inspect remote module ext4 source image",
                category=ErrorCategory.BUILD_FAILURE,
                details={"tool": "debugfs"},
            ) from exc
        return destination

    def inspect(self, path: Path) -> SourceFilesystemEvidence:
        temporary = tempfile.TemporaryDirectory(prefix="kdive-module-readback-", dir=self._work_dir)
        with temporary as raw:
            extracted = self.extract(path, Path(raw))
            operation = (extracted / "operation-v1.json").read_bytes()
            manifest, count, content_bytes = _source_tree_manifest(extracted / "modules")
        return SourceFilesystemEvidence(operation, manifest, count, content_bytes)


@dataclass(frozen=True, slots=True)
class VolumeRequest:
    pool: str
    system_id: str
    run_id: str
    operation_nonce: str
    operation: RemoteModuleOperationV1
    source_manifest: str
    entries: tuple[ModuleTreeEntry, ...]
    writer: FilesystemImageWriter
    inspect_attachments: Callable[[], AttachmentInspection]
    # Readback lands the whole source image on disk, so it belongs in the
    # configured work directory rather than the default temp dir, which on a
    # systemd host is a RAM-backed tmpfs sized at half of memory.
    work_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class PreparedVolume:
    pool: str
    name: str
    system_id: str
    run_id: str
    operation_nonce: str
    purpose: str
    digest: str
    capacity_bytes: int


@dataclass(frozen=True, slots=True)
class PreparedModuleVolumes:
    source: PreparedVolume
    scratch: PreparedVolume


class Volume(Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802
    def info(self) -> list[int]: ...
    def path(self) -> str: ...
    def upload(self, stream: object, offset: int, length: int, flags: int = 0) -> int: ...
    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int: ...
    def delete(self, flags: int = 0) -> int: ...


class Pool(Protocol):
    def storageVolLookupByName(self, name: str) -> Volume: ...  # noqa: N802
    def createXML(self, xml: str, flags: int = 0) -> Volume: ...  # noqa: N802


class UploadStream(Protocol):
    def sendAll(  # noqa: N802
        self, handler: Callable[[object, int, object], bytes], opaque: object
    ) -> None: ...
    def recvAll(  # noqa: N802
        self, handler: Callable[[object, bytes, object], None], opaque: object
    ) -> None: ...
    def finish(self) -> int: ...
    def abort(self) -> int: ...


class StorageConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> Pool: ...  # noqa: N802
    def newStream(self, flags: int = 0) -> UploadStream: ...  # noqa: N802


def _conflict(message: str, **details: object) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFLICT, details=details)


def _names(request: VolumeRequest) -> tuple[str, str]:
    return (
        render_module_volume_name(
            request.system_id, request.run_id, request.operation_nonce, "source.ext4"
        ),
        render_module_volume_name(
            request.system_id, request.run_id, request.operation_nonce, "scratch.ext4"
        ),
    )


def _validate_entries(entries: tuple[ModuleTreeEntry, ...]) -> None:
    content_bytes = 0
    seen: set[str] = set()
    for entry in entries:
        path = PurePosixPath(entry.path)
        if path.is_absolute() or not entry.path or ".." in path.parts or entry.path in seen:
            raise ValueError(f"unsafe module-tree path: {entry.path!r}")
        seen.add(entry.path)
        kind = stat.S_IFMT(entry.mode)
        if kind == stat.S_IFREG:
            if entry.content is None or entry.link_target is not None:
                raise ValueError("regular module-tree entries require bytes only")
            content_bytes += len(entry.content)
        elif kind == stat.S_IFLNK:
            target = entry.link_target
            if entry.content is not None or target is None:
                raise ValueError("symlink module-tree entries require a target only")
            target_path = PurePosixPath(target)
            if target_path.is_absolute() or ".." in target_path.parts:
                raise ValueError("module-tree symlink target escapes /modules")
        elif kind != stat.S_IFDIR:
            raise ValueError("special module-tree entries are forbidden")
        validate_module_tree_bounds(entry_count=len(entries), content_bytes=content_bytes)


def validate_module_tree_bounds(*, entry_count: int, content_bytes: int) -> None:
    """Validate source limits without requiring materialized boundary-sized payloads."""
    if entry_count > MAX_ENTRIES:
        raise ValueError(f"module tree exceeds {MAX_ENTRIES} entries")
    if content_bytes > MAX_CONTENT_BYTES:
        raise ValueError(f"module tree exceeds {MAX_CONTENT_BYTES} content bytes")


def source_image_capacity_bytes(content_bytes: int, entry_count: int) -> int:
    """Return a block-aligned ext4 size with bounded metadata and growth headroom."""
    validate_module_tree_bounds(entry_count=entry_count, content_bytes=content_bytes)
    inode_bytes = entry_count * _SOURCE_BLOCK_BYTES
    growth_headroom = (content_bytes + _SOURCE_HEADROOM_DIVISOR - 1) // _SOURCE_HEADROOM_DIVISOR
    required = content_bytes + inode_bytes + _SOURCE_FIXED_OVERHEAD_BYTES + growth_headroom
    aligned = (required + _SOURCE_BLOCK_BYTES - 1) // _SOURCE_BLOCK_BYTES * _SOURCE_BLOCK_BYTES
    return max(_MIN_SOURCE_CAPACITY_BYTES, aligned)


def source_image_inode_count(entry_count: int) -> int:
    """Provision every module entry plus safe root/layout and ext4 housekeeping overhead."""
    validate_module_tree_bounds(entry_count=entry_count, content_bytes=0)
    return entry_count + _SOURCE_ROOT_INODES


def _source_tree_manifest(root: Path) -> tuple[str, int, int]:
    documents: list[dict[str, object]] = []
    count = 0
    content_bytes = 0
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        current = Path(directory)
        names = sorted((*subdirectories, *files), key=lambda value: value.encode())
        for name in names:
            path = current / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            count += 1
            if count > MAX_ENTRIES:
                raise ValueError(f"module tree exceeds {MAX_ENTRIES} entries")
            if stat.S_ISDIR(metadata.st_mode):
                document: dict[str, object] = dict(mode="0755", path=relative, type="dir")
            elif stat.S_ISREG(metadata.st_mode):
                payload = path.read_bytes()
                content_bytes += len(payload)
                document = dict(
                    mode="0755" if metadata.st_mode & 0o111 else "0644",
                    path=relative,
                    sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
                    size=len(payload),
                    type="file",
                )
            elif stat.S_ISLNK(metadata.st_mode):
                document = dict(
                    mode="0777", path=relative, target=os.readlink(path), type="symlink"
                )
                if name in subdirectories:
                    subdirectories.remove(name)
            else:
                raise ValueError("special module-tree entries are forbidden")
            documents.append(cast("dict[str, object]", document))
    documents.sort(key=lambda value: str(value["path"]).encode())
    encoded = json.dumps(
        {"entries": documents, "schema": "module-source-manifest-v1"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(b"kdive-module-source-manifest-v1\0" + encoded).hexdigest()
    return f"sha256:{digest}", count, content_bytes


def _render_volume(name: str, capacity: int) -> str:
    root = ET.Element("volume")
    ET.SubElement(root, "name").text = name
    ET.SubElement(root, "capacity", unit="bytes").text = str(capacity)
    target = ET.SubElement(root, "target")
    ET.SubElement(target, "format", type="raw")
    return ET.tostring(root, encoding="unicode")


def _lookup(pool: Pool, name: str) -> Volume | None:
    try:
        return pool.storageVolLookupByName(name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
            return None
        raise CategorizedError(
            "failed to inspect remote module volume",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"volume": name},
        ) from exc


def _lookup_pool(conn: StorageConn, pool_name: str) -> Pool:
    try:
        return conn.storagePoolLookupByName(pool_name)
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "failed to look up remote module storage pool",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"pool": pool_name},
        ) from exc


def protected_volume_paths(
    conn: StorageConn, pool_name: str, names: tuple[str, ...]
) -> frozenset[str]:
    """Resolve the backing paths of whichever protected volumes exist right now.

    A `<disk type='volume'>` is indirection libvirt resolves to one of these
    paths, so exclusivity cannot be proved from pool/volume pairs alone. An
    absent volume contributes no path because there is nothing yet to protect;
    an unresolvable one fails closed.
    """
    pool = _lookup_pool(conn, pool_name)
    paths: set[str] = set()
    for name in names:
        volume = _lookup(pool, name)
        if volume is None:
            continue
        try:
            paths.add(volume.path())
        except libvirt.libvirtError as exc:
            raise _conflict("could not resolve a remote module volume path", volume=name) from exc
    return frozenset(paths)


def _readback_facts(volume: Volume, name: str) -> tuple[str, int]:
    try:
        root = parse_libvirt_xml(volume.XMLDesc(0))
        names = root.findall("./name")
        if len(names) != 1 or names[0].text is None:
            raise ValueError("volume name is absent")
        volume_formats = root.findall("./target/format")
        if len(volume_formats) != 1 or volume_formats[0].attrib != {"type": "raw"}:
            raise ValueError("volume is not raw")
        capacity = int(volume.info()[1])
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "failed to read remote module volume facts",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"volume": name},
        ) from exc
    except (DefusedXmlException, ET.ParseError, KeyError, ValueError, IndexError) as exc:
        raise _conflict("remote module volume facts are invalid", volume=name) from exc
    return names[0].text, capacity


def _expected_owner(request: VolumeRequest, purpose: str) -> ModuleVolumeOwner:
    return ModuleVolumeOwner(
        system_id=request.system_id,
        run_id=request.run_id,
        operation_nonce=request.operation_nonce,
        kind=f"{purpose}.ext4",
    )


def _require_owner(observed_name: str, expected: ModuleVolumeOwner, name: str) -> None:
    if parse_module_volume_name(observed_name) != expected:
        raise _conflict("remote module volume ownership mismatched", volume=name)


def _create(pool: Pool, xml: str, name: str) -> Volume:
    try:
        return pool.createXML(xml, 0)
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "failed to create remote module volume",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"volume": name},
        ) from exc


def _admit(admission: Callable[[], None] | None) -> None:
    if admission is not None:
        admission()


def _call_stream[T](
    call: Callable[[Callable[[], object]], object] | None, operation: Callable[[], T]
) -> T:
    if call is None:
        return operation()
    return cast(T, call(cast(Callable[[], object], operation)))


def _abort_stream(
    call: Callable[[Callable[[], object]], object] | None,
    stream: UploadStream,
    primary: BaseException,
) -> None:
    try:
        _call_stream(call, stream.abort)
    except Exception as cleanup:
        primary.add_note(f"stream abort cleanup unresolved: {cleanup!r}")


def _upload(
    conn: StorageConn,
    volume: Volume,
    image: BuiltSourceImage,
    admission: Callable[[], None] | None,
    call_stream: Callable[[Callable[[], object]], object] | None,
) -> None:
    stream: UploadStream | None = None

    def bounded[T](operation: Callable[[], T]) -> T:
        if call_stream is None:
            _admit(admission)
        return _call_stream(call_stream, operation)

    try:
        stream = bounded(lambda: conn.newStream(0))
        bounded(lambda: volume.upload(stream, 0, image.capacity_bytes, 0))
        with image.path.open("rb") as handle:
            bounded(
                lambda: stream.sendAll(lambda _stream, count, _opaque: handle.read(count), None),
            )
        bounded(stream.finish)
    except TimeoutError:
        raise
    except (OSError, libvirt.libvirtError) as exc:
        if stream is not None:
            _abort_stream(call_stream, stream, exc)
        raise CategorizedError(
            "failed to upload remote module source volume",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc


def _source_repair(
    conn: StorageConn,
    request: VolumeRequest,
    image: BuiltSourceImage,
    admission: Callable[[], None] | None,
    call_stream: Callable[[Callable[[], object]], object] | None,
) -> Callable[[Volume], bool]:
    """Finish an upload a dead worker left partial, for an already-owned volume.

    A source volume's existence does not prove its upload completed: a worker
    that died between the create and the end of the upload leaves correct owner
    metadata over empty or partial content. Without repair the same-nonce retry
    skips the upload forever and fails the readback on every attempt, and the
    attempt cannot be reaped either, because reaping needs a durable result the
    appliance never wrote.

    Repair demands the same detachment proof a delete does, so nothing writes
    into a volume a live appliance still holds. Declining returns False so the
    caller re-raises the original readback failure, which is the accurate one.
    """

    def repair(volume: Volume) -> bool:
        if not request.inspect_attachments().proves_detached(request.pool, _names(request)[0]):
            return False
        _admit(admission)
        _upload(conn, volume, image, admission, call_stream)
        return True

    return repair


def _inspect_remote_source(
    conn: StorageConn,
    volume: Volume,
    request: VolumeRequest,
    expected: BuiltSourceImage,
    call_stream: Callable[[Callable[[], object]], object] | None = None,
) -> None:
    stream: UploadStream | None = None
    descriptor, raw_path = tempfile.mkstemp(
        prefix="kdive-module-volume-", suffix=".ext4", dir=request.work_dir
    )
    path = Path(raw_path)
    received = 0
    try:
        stream = conn.newStream(0)
        with os.fdopen(descriptor, "wb") as handle:

            def receive(_stream: object, chunk: bytes, _opaque: object) -> None:
                nonlocal received
                received += len(chunk)
                if received > expected.capacity_bytes:
                    raise ValueError("download exceeded expected source capacity")
                handle.write(chunk)

            _call_stream(
                call_stream,
                lambda: volume.download(stream, 0, expected.capacity_bytes, 0),
            )
            _call_stream(call_stream, lambda: stream.recvAll(receive, None))
            _call_stream(call_stream, stream.finish)
        if received != expected.capacity_bytes:
            raise _conflict("remote module source readback is truncated")
        try:
            observed = request.writer.inspect(path)
        except CategorizedError as exc:
            raise _conflict("remote module source readback failed") from exc
        if observed != expected.evidence:
            raise _conflict("remote module source filesystem readback mismatched")
    except CategorizedError:
        raise
    except libvirt.libvirtError as exc:
        if stream is not None:
            _abort_stream(call_stream, stream, exc)
        raise CategorizedError(
            "failed to download remote module source volume",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        if stream is not None and not isinstance(exc, TimeoutError):
            _abort_stream(call_stream, stream, exc)
        raise _conflict("remote module source readback failed") from exc
    finally:
        path.unlink(missing_ok=True)


def _prepared(
    request: VolumeRequest,
    name: str,
    purpose: str,
    digest: str,
    capacity: int,
) -> PreparedVolume:
    return PreparedVolume(
        pool=request.pool,
        name=name,
        system_id=request.system_id,
        run_id=request.run_id,
        operation_nonce=request.operation_nonce,
        purpose=purpose,
        digest=digest,
        capacity_bytes=capacity,
    )


def prepare_attempt_volumes(
    conn: StorageConn,
    request: VolumeRequest,
    *,
    admit_mutation: Callable[[], None] | None = None,
    call_stream: Callable[[Callable[[], object]], object] | None = None,
) -> PreparedModuleVolumes:
    """Create or exactly validate the two deterministic volumes for one attempt."""
    _validate_entries(request.entries)
    source_name, scratch_name = _names(request)
    pool = _lookup_pool(conn, request.pool)
    existing_source = _lookup(pool, source_name)
    existing_scratch = _lookup(pool, scratch_name)
    operation = request.operation.to_wire_bytes()
    image = request.writer.build(operation, request.entries)
    try:
        observed = request.writer.inspect(image.path)
        expected_content_bytes = sum(len(entry.content or b"") for entry in request.entries)
        if observed != image.evidence or observed.operation != operation:
            raise _conflict("source filesystem readback does not match its input")
        if (
            observed.manifest != request.source_manifest
            or observed.entry_count != len(request.entries)
            or observed.content_bytes != expected_content_bytes
        ):
            raise _conflict("source filesystem manifest or bounds readback mismatched")
        source_capacity = image.capacity_bytes
        source = _prepared(request, source_name, "source", request.source_manifest, source_capacity)
        scratch = _prepared(
            request,
            scratch_name,
            "scratch",
            "sha256:" + "0" * 64,
            SCRATCH_CAPACITY_BYTES,
        )
        created: list[PreparedVolume] = []
        reused_source = existing_source is not None
        if existing_source is None:
            _admit(admit_mutation)
            existing_source = _create(
                pool,
                _render_volume(source.name, source.capacity_bytes),
                source.name,
            )
            created.append(source)
            _upload(conn, existing_source, image, admit_mutation, call_stream)
        if existing_scratch is None:
            _admit(admit_mutation)
            existing_scratch = _create(
                pool,
                _render_volume(scratch.name, scratch.capacity_bytes),
                scratch.name,
            )
            created.append(scratch)
        # validate_attempt_volumes re-proves ownership, identity, capacity and
        # the source content, so this needs no separate readback of its own.
        return validate_attempt_volumes(
            conn,
            request,
            source=source,
            scratch=scratch,
            call_stream=call_stream,
            repair_source=(
                _source_repair(conn, request, image, admit_mutation, call_stream)
                if reused_source
                else None
            ),
        )
    except Exception:
        try:
            inspection = request.inspect_attachments()
        except Exception:
            inspection = None
        if inspection is not None:
            for volume in reversed(created):
                with contextlib.suppress(Exception):
                    _admit(admit_mutation)
                    delete_owned_attempt_volume(
                        conn,
                        volume,
                        inspection=inspection,
                        admit_mutation=admit_mutation,
                    )
        raise
    finally:
        image.path.unlink(missing_ok=True)


def validate_attempt_volumes(
    conn: StorageConn,
    request: VolumeRequest,
    *,
    source: PreparedVolume | None = None,
    scratch: PreparedVolume | None = None,
    call_stream: Callable[[Callable[[], object]], object] | None = None,
    repair_source: Callable[[Volume], bool] | None = None,
) -> PreparedModuleVolumes:
    """Reopen and identity-check both volumes before use or retry."""
    _validate_entries(request.entries)
    expected_image = request.writer.build(request.operation.to_wire_bytes(), request.entries)
    try:
        if source is None:
            source_name, _scratch_name = _names(request)
            source = _prepared(
                request,
                source_name,
                "source",
                request.source_manifest,
                expected_image.capacity_bytes,
            )
        pool = _lookup_pool(conn, request.pool)
        source_name, scratch_name = _names(request)
        result: list[PreparedVolume] = []
        for name, purpose, digest, capacity in (
            (
                source_name,
                "source",
                request.source_manifest,
                expected_image.capacity_bytes,
            ),
            (scratch_name, "scratch", "sha256:" + "0" * 64, SCRATCH_CAPACITY_BYTES),
        ):
            volume = _lookup(pool, name)
            if volume is None:
                raise _conflict("owned remote module volume is absent", volume=name)
            observed_name, actual_capacity = _readback_facts(volume, name)
            _require_owner(observed_name, _expected_owner(request, purpose), name)
            if actual_capacity != capacity:
                raise _conflict("remote module volume capacity mismatched", volume=name)
            prior = source if purpose == "source" else scratch
            expected_prepared = PreparedVolume(
                request.pool,
                name,
                request.system_id,
                request.run_id,
                request.operation_nonce,
                purpose,
                digest,
                actual_capacity,
            )
            if prior is not None and prior != expected_prepared:
                raise _conflict("provided remote module volume identity mismatched", volume=name)
            result.append(expected_prepared)
        prepared = PreparedModuleVolumes(*result)
        source_volume = _lookup(pool, source_name)
        assert source_volume is not None
        try:
            _inspect_remote_source(conn, source_volume, request, expected_image, call_stream)
        except CategorizedError as unusable:
            # Only a content failure is repairable, and only once ownership,
            # identity and capacity above have proved the volume is ours.
            if repair_source is None or unusable.category is not ErrorCategory.CONFLICT:
                raise
            if not repair_source(source_volume):
                raise
            _inspect_remote_source(conn, source_volume, request, expected_image, call_stream)
        return prepared
    finally:
        expected_image.path.unlink(missing_ok=True)


def validate_scratch_volume(conn: StorageConn, request: VolumeRequest) -> PreparedVolume:
    """Reopen the durable scratch volume without requiring the disposable source."""
    _validate_entries(request.entries)
    _source_name, scratch_name = _names(request)
    expected = _prepared(
        request, scratch_name, "scratch", "sha256:" + "0" * 64, SCRATCH_CAPACITY_BYTES
    )
    volume = _lookup(_lookup_pool(conn, request.pool), scratch_name)
    if volume is None:
        raise _conflict("owned remote module scratch volume is absent", volume=scratch_name)
    observed_name, capacity = _readback_facts(volume, scratch_name)
    _require_owner(observed_name, _expected_owner(request, "scratch"), scratch_name)
    if capacity != SCRATCH_CAPACITY_BYTES:
        raise _conflict(
            "remote module scratch ownership or capacity mismatched", volume=scratch_name
        )
    return expected


def expected_attempt_volumes(request: VolumeRequest) -> PreparedModuleVolumes:
    """Derive immutable volume identities without requiring either volume to exist."""
    image = request.writer.build(request.operation.to_wire_bytes(), request.entries)
    try:
        source_name, scratch_name = _names(request)
        return PreparedModuleVolumes(
            _prepared(
                request,
                source_name,
                "source",
                request.source_manifest,
                image.capacity_bytes,
            ),
            _prepared(
                request,
                scratch_name,
                "scratch",
                "sha256:" + "0" * 64,
                SCRATCH_CAPACITY_BYTES,
            ),
        )
    finally:
        image.path.unlink(missing_ok=True)


def delete_owned_attempt_volume(
    conn: StorageConn,
    volume: PreparedVolume,
    *,
    inspection: AttachmentInspection,
    admit_mutation: Callable[[], None] | None = None,
) -> None:
    """Delete only an exactly owned, detached volume and verify absence."""
    if not inspection.proves_detached(volume.pool, volume.name):
        raise _conflict("volume lacks exact detached attachment proof", volume=volume.name)
    pool = _lookup_pool(conn, volume.pool)
    found = _lookup(pool, volume.name)
    if found is None:
        return
    expected_owner = ModuleVolumeOwner(
        volume.system_id,
        volume.run_id,
        volume.operation_nonce,
        f"{volume.purpose}.ext4",
    )
    observed_name, capacity = _readback_facts(found, volume.name)
    _require_owner(observed_name, expected_owner, volume.name)
    if capacity != volume.capacity_bytes:
        raise _conflict("refusing to delete an unowned remote module volume", volume=volume.name)
    try:
        _admit(admit_mutation)
        found.delete(0)
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "failed to delete remote module volume",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"volume": volume.name},
        ) from exc
    if _lookup(pool, volume.name) is not None:
        raise _conflict("remote module volume remained after deletion", volume=volume.name)
