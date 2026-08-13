"""Post-release capture child spool boundary (ADR-0558)."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from hashlib import sha256

_REQUEST_NAME = "request.json"
_REQUEST_DIGEST_NAME = "request.sha256"
_RESULT_NAME = "result.json"
_MAX_REQUEST_BYTES = 16_384


def _read_bounded(fd: int, maximum: int) -> bytes:
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
        raise ValueError(f"capture spool file exceeds {maximum} bytes")
    return data


def _read_private_file(directory_fd: int, name: str, maximum: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"capture spool {name} is not a regular file")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError(f"capture spool {name} must be owner-owned mode 0600")
        if metadata.st_size > maximum:
            raise ValueError(f"capture spool {name} exceeds {maximum} bytes")
        return _read_bounded(fd, maximum)
    finally:
        os.close(fd)


def _write_private_result(directory_fd: int, data: bytes) -> None:
    temporary = ".result.json.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise
    finally:
        os.close(fd)
    os.rename(temporary, _RESULT_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    os.fsync(directory_fd)


def _open_attempt_directory() -> int:
    metadata = os.stat(".", follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("capture attempt cwd is not a directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PermissionError("capture attempt cwd must be owner-owned mode 0700")
    return os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)


def run_capture_child(launch_token: str, gate_fd: int) -> int:
    """Validate the released request and write a bounded placeholder result.

    Task 3 replaces the placeholder with provider execution. Keeping this post-release path real
    lets Task 2 prove request/spool containment without crossing a provider boundary early.
    """
    invalid_character = any(character not in "0123456789abcdef" for character in launch_token)
    if len(launch_token) != 64 or invalid_character:
        raise ValueError("launch token must be 64 lowercase hexadecimal characters")
    with suppress(OSError):
        os.close(gate_fd)
    directory_fd = _open_attempt_directory()
    try:
        from kdive.domain.errors import ErrorCategory
        from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult

        request_bytes = _read_private_file(directory_fd, _REQUEST_NAME, _MAX_REQUEST_BYTES)
        expected_digest = _read_private_file(directory_fd, _REQUEST_DIGEST_NAME, 65)
        if expected_digest != (sha256(request_bytes).hexdigest() + "\n").encode():
            raise ValueError("capture request digest does not match its spool attestation")
        CaptureRequest.from_canonical_json(request_bytes)
        result = CaptureResult(
            outcome="failure",
            size_bytes=0,
            truncated=False,
            error_category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            terminal=False,
            reason="provider_execution_not_installed",
            details={"phase": "capture_child"},
        )
        _write_private_result(directory_fd, result.to_canonical_json())
    finally:
        os.close(directory_fd)
    return 0
