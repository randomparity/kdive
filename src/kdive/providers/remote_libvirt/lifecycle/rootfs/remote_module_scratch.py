"""Format the remote module appliance's scratch volume exactly once (ADR-0585).

The appliance mounts ``/dev/vdc`` as ext4 and writes its phase checkpoints and result there, so
the volume must carry a filesystem before the appliance first starts. The host builds a sparse
ext4 image with ``mkfs.ext4`` and uploads only its allocated extents. The first block, which holds
the superblock, is uploaded last: a worker that dies mid-upload leaves a volume whose first block
is still zero, which a retry recognises as unformatted and finishes. A volume whose first block
already carries the ext4 magic is never written again, because it may hold an ADR-0585 phase.
"""

from __future__ import annotations

import errno
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import BinaryIO, Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory

HEAD_BYTES = 4096
_EXT4_MAGIC_OFFSET = 1080
_EXT4_MAGIC = b"\x53\xef"
_MKFS_TIMEOUT_SECONDS = 120
_MAX_EXTENTS = 4096
_MAX_UPLOAD_BYTES = 1024**3


class ScratchState(Enum):
    FORMATTED = auto()
    BLANK = auto()
    FOREIGN = auto()


class _Stream(Protocol):
    def sendAll(  # noqa: N802
        self, handler: Callable[[object, int, object], bytes], opaque: object
    ) -> None: ...
    def recvAll(  # noqa: N802
        self, handler: Callable[[object, bytes, object], None], opaque: object
    ) -> None: ...
    def finish(self) -> int: ...
    def abort(self) -> int: ...


class _Volume(Protocol):
    def upload(self, stream: object, offset: int, length: int, flags: int = 0) -> int: ...
    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int: ...


class _Conn(Protocol):
    def newStream(self, flags: int = 0) -> _Stream: ...  # noqa: N802


class Bounded(Protocol):
    def __call__[T](self, operation: Callable[[], T]) -> T: ...


@dataclass(frozen=True, slots=True)
class StreamIo:
    """Bound every libvirt call the same way the volume code does, and abort a failed stream."""

    bounded: Bounded
    abort: Callable[[_Stream, BaseException], None]


def classify_head(head: bytes) -> ScratchState:
    """Classify the first block of a scratch volume."""
    if len(head) != HEAD_BYTES:
        return ScratchState.FOREIGN
    if head[_EXT4_MAGIC_OFFSET : _EXT4_MAGIC_OFFSET + 2] == _EXT4_MAGIC:
        return ScratchState.FORMATTED
    if not any(head):
        return ScratchState.BLANK
    return ScratchState.FOREIGN


def read_head(conn: _Conn, volume: _Volume, io: StreamIo) -> bytes:
    """Read the first block of the scratch volume."""
    chunks = bytearray()

    def receive(_stream: object, chunk: bytes, _opaque: object) -> None:
        if len(chunks) + len(chunk) > HEAD_BYTES:
            raise ValueError("scratch head download exceeded its bound")
        chunks.extend(chunk)

    stream: _Stream | None = None
    try:
        stream = io.bounded(lambda: conn.newStream(0))
        opened = stream
        io.bounded(lambda: volume.download(opened, 0, HEAD_BYTES, 0))
        io.bounded(lambda: opened.recvAll(receive, None))
        io.bounded(opened.finish)
    except TimeoutError:
        raise
    except (libvirt.libvirtError, ValueError) as exc:
        if stream is not None:
            io.abort(stream, exc)
        raise CategorizedError(
            "failed to read the remote module scratch volume",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
    return bytes(chunks)


def build_scratch_image(capacity_bytes: int, work_dir: Path | None) -> Path:
    """Build a sparse ext4 image of ``capacity_bytes``; the caller unlinks it."""
    descriptor, raw = tempfile.mkstemp(prefix="kdive-module-scratch-", suffix=".ext4", dir=work_dir)
    image = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.truncate(capacity_bytes)
        subprocess.run(  # noqa: S603 - fixed executable and closed argument vector
            [
                "mkfs.ext4",  # noqa: S607
                "-q",
                "-F",
                "-b",
                "4096",
                "-E",
                "lazy_itable_init=1,lazy_journal_init=1",
                str(image),
            ],
            check=True,
            capture_output=True,
            timeout=_MKFS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        image.unlink(missing_ok=True)
        raise CategorizedError(
            "failed to build the remote module scratch filesystem",
            category=ErrorCategory.BUILD_FAILURE,
            details={"tool": "mkfs.ext4"},
        ) from exc
    return image


def _data_extents(image: Path) -> list[tuple[int, int]]:
    """Return the allocated ``(offset, length)`` extents of a sparse file, in order."""
    size = image.stat().st_size
    extents: list[tuple[int, int]] = []
    with image.open("rb") as handle:
        descriptor = handle.fileno()
        position = 0
        while position < size:
            try:
                start = os.lseek(descriptor, position, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                raise CategorizedError(
                    "failed to enumerate the remote module scratch filesystem",
                    category=ErrorCategory.BUILD_FAILURE,
                ) from exc
            end = os.lseek(descriptor, start, os.SEEK_HOLE)
            extents.append((start, end - start))
            position = end
            if len(extents) > _MAX_EXTENTS:
                raise CategorizedError(
                    "remote module scratch filesystem is too fragmented to upload",
                    category=ErrorCategory.BUILD_FAILURE,
                )
    return extents


def _body_extents(image: Path) -> list[tuple[int, int]]:
    """Allocated extents past the first block, which is uploaded separately and last."""
    body = []
    for offset, length in _data_extents(image):
        start = max(offset, HEAD_BYTES)
        if offset + length > start:
            body.append((start, offset + length - start))
    return body


def _upload_range(
    conn: _Conn, volume: _Volume, reader: BinaryIO, offset: int, length: int, io: StreamIo
) -> None:
    remaining = length

    def send(_stream: object, count: int, _opaque: object) -> bytes:
        nonlocal remaining
        chunk = reader.read(min(count, remaining))
        remaining -= len(chunk)
        return bytes(chunk)

    stream: _Stream | None = None
    try:
        reader.seek(offset)
        stream = io.bounded(lambda: conn.newStream(0))
        opened = stream
        io.bounded(lambda: volume.upload(opened, offset, length, 0))
        io.bounded(lambda: opened.sendAll(send, None))
        if remaining:
            raise OSError("scratch filesystem image ended before its extent did")
        io.bounded(opened.finish)
    except TimeoutError:
        raise
    except (OSError, libvirt.libvirtError) as exc:
        if stream is not None:
            io.abort(stream, exc)
        raise CategorizedError(
            "failed to upload the remote module scratch filesystem",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc


def format_scratch_volume(
    conn: _Conn,
    volume: _Volume,
    capacity_bytes: int,
    work_dir: Path | None,
    io: StreamIo,
) -> None:
    """Upload a fresh ext4 filesystem, writing the superblock block last."""
    image = build_scratch_image(capacity_bytes, work_dir)
    try:
        extents = _body_extents(image)
        if sum(length for _offset, length in extents) + HEAD_BYTES > _MAX_UPLOAD_BYTES:
            raise CategorizedError(
                "remote module scratch filesystem metadata exceeds its upload bound",
                category=ErrorCategory.BUILD_FAILURE,
            )
        with image.open("rb") as handle:
            for offset, length in extents:
                _upload_range(conn, volume, handle, offset, length, io)
            _upload_range(conn, volume, handle, 0, HEAD_BYTES, io)
    finally:
        image.unlink(missing_ok=True)
