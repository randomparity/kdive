"""Bounded initrd archive rewrites used while normalizing catalog root filesystems."""

from __future__ import annotations

import os
import stat
import tempfile
from compression import zstd
from pathlib import Path
from typing import Protocol

_NEWC_HEADER_SIZE = 110
_COPY_CHUNK_SIZE = 1024 * 1024
_MAX_NAME_SIZE = 4096
_MAX_UNCOMPRESSED_SIZE = 2 * 1024 * 1024 * 1024


class _Readable(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class _Writable(Protocol):
    def write(self, data: bytes, /) -> int: ...


class _BoundedReader:
    """Limit decompressed input so a malformed frame cannot expand without bound."""

    def __init__(self, source: _Readable) -> None:
        self._source = source
        self._remaining = _MAX_UNCOMPRESSED_SIZE

    def read(self, size: int) -> bytes:
        data = self._source.read(min(size, self._remaining + 1))
        if len(data) > self._remaining:
            raise ValueError("initrd exceeds the 2 GiB decompressed-size limit")
        self._remaining -= len(data)
        return data


def _read_exact(source: _BoundedReader, size: int, description: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = source.read(remaining)
        if not chunk:
            raise ValueError(f"truncated initrd {description}")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _copy_exact(source: _BoundedReader, destination: _Writable | None, size: int) -> None:
    remaining = size
    while remaining:
        chunk = _read_exact(source, min(remaining, _COPY_CHUNK_SIZE), "entry data")
        if destination is not None:
            destination.write(chunk)
        remaining -= len(chunk)


def _hex_field(header: bytes, index: int, name: str) -> int:
    start = 6 + index * 8
    try:
        return int(header[start : start + 8], 16)
    except ValueError as exc:
        raise ValueError(f"invalid newc {name} field") from exc


def _rewrite_newc(source: _BoundedReader, destination: _Writable, target: bytes) -> bool:
    """Copy a newc stream byte-for-byte except for one exact path."""
    removed = False
    while True:
        header = _read_exact(source, _NEWC_HEADER_SIZE, "newc header")
        if header[:6] not in (b"070701", b"070702"):
            raise ValueError("invalid initrd newc header")
        file_size = _hex_field(header, 6, "file size")
        name_size = _hex_field(header, 11, "name size")
        if not 1 <= name_size <= _MAX_NAME_SIZE:
            raise ValueError("invalid newc name size")
        name_padding = -(len(header) + name_size) % 4
        name_block = _read_exact(source, name_size + name_padding, "entry name")
        if name_block[name_size - 1] != 0:
            raise ValueError("newc entry name is not NUL-terminated")
        name = name_block[: name_size - 1]
        data_size = file_size + (-file_size % 4)
        omit = name == target
        if omit and removed:
            raise ValueError("initrd contains duplicate target entries")
        if not omit:
            destination.write(header)
            destination.write(name_block)
        _copy_exact(source, None if omit else destination, data_size)
        removed = removed or omit
        if name == b"TRAILER!!!":
            while chunk := source.read(_COPY_CHUNK_SIZE):
                destination.write(chunk)
            return removed


def remove_zstd_newc_entry(path: Path, entry: str) -> bool:
    """Atomically remove ``entry`` from a zstd-compressed newc initrd.

    Returns ``False`` without changing ``path`` when the entry is absent. The decompressed stream
    is capped at 2 GiB and copied incrementally so normal initrds do not scale memory use with their
    uncompressed size.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as out:
        replacement = Path(out.name)
    try:
        with zstd.open(path, "rb") as compressed_source, zstd.open(replacement, "wb") as output:
            removed = _rewrite_newc(
                _BoundedReader(compressed_source), output, entry.removeprefix("/").encode()
            )
        if not removed:
            replacement.unlink()
            return False
        replacement.chmod(mode)
        os.replace(replacement, path)
        return True
    except BaseException:
        replacement.unlink(missing_ok=True)
        raise
