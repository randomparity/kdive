"""No-follow ownership and content attestation for capture bootstrap files."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_DIRECTORY_FLAGS = _READ_FLAGS | os.O_DIRECTORY


def _approved(metadata: os.stat_result, expected_uid: int) -> bool:
    return metadata.st_uid in {0, expected_uid}


def _open_component(parent_fd: int, name: str, *, directory: bool) -> int:
    flags = _DIRECTORY_FLAGS if directory else _READ_FLAGS
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise PermissionError(
            "capture bootstrap fingerprint path is not safely openable"
        ) from error


def _verify_ancestor(
    metadata: os.stat_result,
    child: os.stat_result,
    expected_uid: int,
) -> None:
    mode = metadata.st_mode
    details = (
        "component=capture_manifest_fingerprint_ancestor "
        f"uid={metadata.st_uid} gid={metadata.st_gid} mode={stat.S_IMODE(mode):04o}"
    )
    if not stat.S_ISDIR(mode) or not _approved(metadata, expected_uid):
        raise PermissionError(
            "capture bootstrap fingerprint ancestor rejected: "
            f"reason=fingerprint_ancestor_unapproved_owner {details}"
        )
    writable_by_others = bool(mode & (stat.S_IWGRP | stat.S_IWOTH))
    sticky_protected = bool(mode & stat.S_ISVTX) and _approved(child, expected_uid)
    if writable_by_others and not sticky_protected:
        raise PermissionError(
            "capture bootstrap fingerprint ancestor rejected: "
            f"reason=fingerprint_ancestor_replaceable {details}"
        )


def _open_attested(path: Path, expected_uid: int) -> tuple[int, os.stat_result]:
    if not path.is_absolute() or not path.parts[1:]:
        raise PermissionError("capture bootstrap fingerprint path must be an absolute file path")
    parent_fd = os.open(Path("/"), _DIRECTORY_FLAGS)
    parent_metadata = os.fstat(parent_fd)
    try:
        for component in path.parts[1:-1]:
            child_fd = _open_component(parent_fd, component, directory=True)
            try:
                child_metadata = os.fstat(child_fd)
                _verify_ancestor(parent_metadata, child_metadata, expected_uid)
            except BaseException:
                os.close(child_fd)
                raise
            os.close(parent_fd)
            parent_fd = child_fd
            parent_metadata = child_metadata
        file_fd = _open_component(parent_fd, path.name, directory=False)
        try:
            file_metadata = os.fstat(file_fd)
            _verify_ancestor(parent_metadata, file_metadata, expected_uid)
        except BaseException:
            os.close(file_fd)
            raise
    finally:
        os.close(parent_fd)
    if not stat.S_ISREG(file_metadata.st_mode):
        os.close(file_fd)
        raise PermissionError("capture bootstrap fingerprint must be a regular file")
    if not _approved(file_metadata, expected_uid):
        os.close(file_fd)
        raise PermissionError("capture bootstrap fingerprint file has an unapproved owner")
    if file_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.close(file_fd)
        raise PermissionError("capture bootstrap fingerprint file is group/world writable")
    return file_fd, file_metadata


def _revalidate(path: Path, expected_uid: int, metadata: os.stat_result) -> None:
    verification_fd, verification_metadata = _open_attested(path, expected_uid)
    os.close(verification_fd)
    if (metadata.st_dev, metadata.st_ino) != (
        verification_metadata.st_dev,
        verification_metadata.st_ino,
    ):
        raise RuntimeError("capture bootstrap attested path changed during verification")


def _read_bounded(file_fd: int, maximum_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum_size + 1
    while remaining and (chunk := os.read(file_fd, min(1024 * 1024, remaining))):
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def fingerprint(path: Path, *, expected_uid: int) -> str:
    """Hash one immutable, approved-owned regular file through its attested descriptor."""
    file_fd, metadata = _open_attested(path, expected_uid)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(file_fd, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(file_fd)
    _revalidate(path, expected_uid, metadata)
    return digest.hexdigest()


def read_manifest(path: Path, *, expected_uid: int, maximum_size: int) -> bytes:
    """Read the exact approved-owned manifest object without following path links."""
    file_fd, metadata = _open_attested(path, expected_uid)
    try:
        if metadata.st_uid != expected_uid:
            raise PermissionError("capture bootstrap manifest has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o644:
            raise PermissionError("capture bootstrap manifest must have mode 0644")
        raw = _read_bounded(file_fd, maximum_size)
    finally:
        os.close(file_fd)
    _revalidate(path, expected_uid, metadata)
    if len(raw) > maximum_size:
        raise RuntimeError(f"capture bootstrap manifest exceeds {maximum_size} bytes")
    return raw
