"""Synchronous traffic-capture execution shared by provider runtimes (ADR-0558)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.traffic import (
    CaptureExecutionRequest,
    CaptureExecutionResult,
    TrafficCapturer,
    capture_qom_id,
)

_CAPTURE_NAME = "capture.pcap"
_PCAP_HEADER_BYTES = 24
_POLL_INTERVAL_SECONDS = 0.5


def _write_capture(result_dir: Path, data: bytes) -> None:
    directory_fd = os.open(result_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    temporary = ".capture.pcap.tmp"
    fd = -1
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(temporary, _CAPTURE_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise
    finally:
        os.close(directory_fd)


class CaptureExecutor:
    """Run provider capture primitives inside the single-process child boundary."""

    def __init__(
        self,
        *,
        capturer: TrafficCapturer,
        provider_label: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._capturer = capturer
        self._provider_label = provider_label
        self._sleep = sleep

    def execute(self, request: CaptureExecutionRequest, result_dir: Path) -> CaptureExecutionResult:
        """Capture, detach, fetch, bound, spool, and reclaim synchronously."""
        destination: str | None = None
        active_error: BaseException | None = None
        try:
            destination = self._capturer.prepare(request.system_id, request.job_id)
            data, truncated = self._capture(request, destination)
            if len(data) > request.max_bytes:
                raise CategorizedError(
                    f"{self._provider_label} capture result exceeded its request byte bound",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"max_bytes": request.max_bytes},
                )
            if len(data) < _PCAP_HEADER_BYTES:
                raise CategorizedError(
                    "traffic capture produced no readable pcap",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={
                        "reason": "pcap_not_written",
                        "bytes": len(data),
                        "remediation": self._capturer.write_remediation,
                    },
                )
            reclaim_destination = destination
            destination = None
            self._capturer.reclaim(reclaim_destination)
            _write_capture(result_dir, data)
            return CaptureExecutionResult(size_bytes=len(data), truncated=truncated)
        except BaseException as error:
            active_error = error
            raise
        finally:
            if destination is not None:
                try:
                    self._capturer.reclaim(destination)
                except BaseException:
                    if active_error is None:
                        raise

    def _capture(self, request: CaptureExecutionRequest, destination: str) -> tuple[bytes, bool]:
        qom_id = capture_qom_id(request.job_id)
        try:
            self._capturer.attach(
                request.domain_name,
                qom_id=qom_id,
                dest_path=destination,
                snaplen=request.snaplen,
            )
            truncated = False
            for _ in range(request.max_polls):
                self._sleep(_POLL_INTERVAL_SECONDS)
                if self._capturer.captured_size(destination) >= request.max_bytes:
                    truncated = True
                    break
        finally:
            self._capturer.detach(request.domain_name, qom_id=qom_id)
        return self._capturer.fetch(destination, max_bytes=request.max_bytes), truncated
