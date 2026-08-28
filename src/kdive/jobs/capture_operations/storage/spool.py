"""Private capture-spool validation, I/O, and disposal."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path
from uuid import UUID

from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult

_MAX_RESULT_BYTES = 65_536
_MAX_CONFIGURATION_BYTES = 16_384
_SPOOL_FILES = frozenset(
    {"request.json", "request.sha256", "configuration.json", "result.json", "capture.pcap"}
)


def _validate_private_directory(path: Path, *, mode: int = 0o700) -> None:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError(f"capture spool directory is not a regular directory: {path}")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != mode:
        raise PermissionError(
            f"capture spool directory must be owner-owned mode {mode:04o}: {path}"
        )


def _write_request(directory_fd: int, request: CaptureRequest) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open("request.json", flags, 0o600, dir_fd=directory_fd)
    try:
        data = request.to_canonical_json()
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    digest_fd = os.open("request.sha256", flags, 0o600, dir_fd=directory_fd)
    try:
        digest = (request.digest + "\n").encode()
        offset = 0
        while offset < len(digest):
            offset += os.write(digest_fd, digest[offset:])
        os.fsync(digest_fd)
    finally:
        os.close(digest_fd)
    os.fsync(directory_fd)


def _write_configuration(attempt_dir: Path, configuration: bytes) -> None:
    if not configuration or len(configuration) > _MAX_CONFIGURATION_BYTES:
        raise ValueError("capture configuration must contain 1..16384 bytes")
    directory_fd = os.open(attempt_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(directory_fd)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PermissionError("capture attempt directory must be owner-owned mode 0700")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open("configuration.json", flags, 0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(configuration):
                offset += os.write(fd, configuration[offset:])
            os.fsync(fd)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink("configuration.json", dir_fd=directory_fd)
            raise
        finally:
            os.close(fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_result(attempt_dir: Path) -> CaptureResult:
    directory_fd = os.open(attempt_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        fd = os.open("result.json", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise PermissionError("capture result is not a regular file")
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError("capture result must be owner-owned mode 0600")
            if metadata.st_size > _MAX_RESULT_BYTES:
                raise ValueError("capture result exceeds 65536 bytes")
            chunks: list[bytes] = []
            remaining = _MAX_RESULT_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
    return CaptureResult.from_canonical_json(data)


def _read_capture(attempt_dir: Path, maximum: int) -> bytes:
    if maximum < 1:
        raise ValueError("capture read maximum must be positive")
    directory_fd = os.open(attempt_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        fd = os.open(
            "capture.pcap", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise PermissionError("capture pcap is not a regular file")
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError("capture pcap must be owner-owned mode 0600")
            if metadata.st_size > maximum:
                raise ValueError(f"capture pcap exceeds {maximum} bytes")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > maximum:
                raise ValueError(f"capture pcap exceeds {maximum} bytes")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _entry_absent(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _open_spool_directory(parent_fd: int, name: str) -> tuple[int | None, bool]:
    try:
        return (
            os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            ),
            False,
        )
    except FileNotFoundError:
        return None, _entry_absent(parent_fd, name)
    except OSError:
        return None, False


def _open_spool_parent(path: Path) -> tuple[int | None, bool]:
    try:
        _validate_private_directory(path)
        return (
            os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW),
            False,
        )
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False


def _remove_spool_entries(directory_fd: int) -> bool:
    try:
        entries = os.listdir(directory_fd)
        if any(entry not in _SPOOL_FILES for entry in entries):
            return False
        for entry in entries:
            try:
                child = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(child.st_mode)
                or child.st_uid != os.geteuid()
                or stat.S_IMODE(child.st_mode) != 0o600
            ):
                return False
            os.unlink(entry, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError:
        return False
    return True


def _dispose_operation_spool(attempt_dir: Path, operation_id: UUID) -> bool:
    if attempt_dir.name != str(operation_id):
        return False
    parent_fd, absent = _open_spool_parent(attempt_dir.parent)
    if parent_fd is None:
        return absent
    try:
        directory_fd, absent = _open_spool_directory(parent_fd, attempt_dir.name)
        if directory_fd is None:
            return absent
        try:
            metadata = os.fstat(directory_fd)
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                return False
            if not _remove_spool_entries(directory_fd):
                return False
        except OSError:
            return False
        finally:
            os.close(directory_fd)
        try:
            os.rmdir(attempt_dir.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError:
            return False
        return _entry_absent(parent_fd, attempt_dir.name)
    finally:
        os.close(parent_fd)
