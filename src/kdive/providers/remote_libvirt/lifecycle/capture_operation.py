"""Synchronous remote capture child execution and independent TLS quiescence (ADR-0558)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.traffic import (
    CaptureExecutionRequest,
    CaptureExecutionResult,
    QuiescenceEvidence,
    TrafficCapturer,
)

_CAPTURE_NAME = "capture.pcap"
_PCAP_HEADER_BYTES = 24
_POLL_INTERVAL_SECONDS = 0.5


class _ProbeDomain(Protocol):
    def qemuMonitorCommand(self, raw: str, flags: int) -> str: ...  # noqa: N802


class _ProbeConnection(Protocol):
    def lookupByName(self, name: str) -> _ProbeDomain: ...  # noqa: N802
    def close(self) -> object: ...


type ConnectionFactory = Callable[[], AbstractContextManager[_ProbeConnection]]


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


class RemoteCaptureExecutor:
    """Run remote-libvirt capture primitives inside the single-process child boundary."""

    def __init__(
        self,
        *,
        capturer: TrafficCapturer,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._capturer = capturer
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
                    "remote capture result exceeded its request byte bound",
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
        qom_id = f"kdive-dump-{request.job_id}"
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


def _ordered_reply(raw: str, expected_id: str) -> object:
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CategorizedError(
            "remote QMP quiescence response was malformed",
            category=ErrorCategory.CONTROL_FAILURE,
        ) from error
    if not isinstance(response, dict) or response.get("id") != expected_id:
        raise CategorizedError(
            "remote QMP transport did not correlate the ordered response",
            category=ErrorCategory.CONTROL_FAILURE,
        )
    if "return" not in response:
        raise CategorizedError(
            "remote QMP quiescence response was inconclusive",
            category=ErrorCategory.CONTROL_FAILURE,
        )
    return response["return"]


class RemoteLibvirtCaptureQuiescence:
    """Cross a fresh Resource-bound TLS connection and prove the exact QOM object absent."""

    def __init__(self, *, resource_id: UUID, connection: ConnectionFactory) -> None:
        self._resource_id = resource_id
        self._connection = connection

    def prove_absent(self, resource_id: UUID, domain_name: str, qom_id: str) -> QuiescenceEvidence:
        """Detach idempotently, then issue a correlated QOM query on one new TLS connection."""
        if resource_id != self._resource_id:
            raise CategorizedError(
                "remote capture quiescence Resource identity mismatch",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        try:
            context = self._connection()
            with context as connection:
                try:
                    domain = connection.lookupByName(domain_name)
                except libvirt.libvirtError as error:
                    raise CategorizedError(
                        "remote domain lookup failed during capture quiescence",
                        category=ErrorCategory.CONTROL_FAILURE,
                    ) from error
                self._detach(domain, qom_id)
                self._query_absence(domain, qom_id)
        except CategorizedError:
            raise
        except (libvirt.libvirtError, OSError, RuntimeError) as error:
            raise CategorizedError(
                "remote libvirt is unreachable for capture quiescence",
                category=ErrorCategory.TRANSPORT_FAILURE,
            ) from error
        return QuiescenceEvidence(
            provider_kind="remote-libvirt",
            resource_id=resource_id,
            domain_name=domain_name,
            qom_id=qom_id,
            result="absent",
            ordering="fresh-qmp-connection",
        )

    @staticmethod
    def _detach(domain: _ProbeDomain, qom_id: str) -> None:
        command_id = f"kdive-detach-{uuid4()}"
        command = {"execute": "object-del", "arguments": {"id": qom_id}, "id": command_id}
        try:
            raw = domain.qemuMonitorCommand(json.dumps(command), 0)
        except libvirt.libvirtError as error:
            message = str(error).lower()
            if "not found" in message or "devicenotfound" in message:
                return
            raise CategorizedError(
                "remote capture detach failed during quiescence",
                category=ErrorCategory.CONTROL_FAILURE,
            ) from error
        _ordered_reply(raw, command_id)

    @staticmethod
    def _query_absence(domain: _ProbeDomain, qom_id: str) -> None:
        command_id = f"kdive-query-{uuid4()}"
        command = {
            "execute": "qom-list",
            "arguments": {"path": "/objects"},
            "id": command_id,
        }
        try:
            raw = domain.qemuMonitorCommand(json.dumps(command), 0)
        except libvirt.libvirtError as error:
            raise CategorizedError(
                "remote capture QOM query failed during quiescence",
                category=ErrorCategory.CONTROL_FAILURE,
            ) from error
        members = _ordered_reply(raw, command_id)
        if not isinstance(members, list):
            raise CategorizedError(
                "remote capture QOM query returned an inconclusive shape",
                category=ErrorCategory.CONTROL_FAILURE,
            )
        member_names: list[str] = []
        for item in members:
            name = item.get("name") if isinstance(item, dict) else None
            member_type = item.get("type") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(name, str)
                or not isinstance(member_type, str)
            ):
                raise CategorizedError(
                    "remote capture QOM query returned an inconclusive shape",
                    category=ErrorCategory.CONTROL_FAILURE,
                )
            member_names.append(name)
        if qom_id in member_names:
            raise CategorizedError(
                "remote capture QOM object is still present",
                category=ErrorCategory.CONTROL_FAILURE,
            )
