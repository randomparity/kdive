"""Post-release child dispatch into provider executors (ADR-0558)."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.capture_operations import child
from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult
from kdive.providers.ports.traffic import CaptureExecutionResult


def _request(provider_kind: str = "local-libvirt") -> CaptureRequest:
    return CaptureRequest.model_validate(
        {
            "job_id": uuid4(),
            "provider_kind": provider_kind,
            "resource_id": uuid4(),
            "system_id": uuid4(),
            "domain_name": "kdive-child",
            "snaplen": 128,
            "max_bytes": 1_048_576,
            "max_polls": 1,
        }
    )


class _Executor:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[CaptureRequest, Path]] = []

    def execute(self, request: CaptureRequest, result_dir: Path) -> CaptureExecutionResult:
        self.calls.append((request, result_dir))
        if self.failure is not None:
            raise self.failure
        (result_dir / "capture.pcap").write_bytes(b"x" * 24)
        os.chmod(result_dir / "capture.pcap", 0o600)
        return CaptureExecutionResult(size_bytes=24, truncated=False)


@pytest.mark.parametrize("provider_kind", ["local-libvirt", "remote-libvirt"])
def test_child_dispatches_provider_only_after_inputs_are_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    request = _request(provider_kind)
    executor = _Executor()
    written: list[bytes] = []
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(child, "_open_attempt_directory", lambda: directory_fd)
    monkeypatch.setattr(child, "read_capture_inputs", lambda _fd: (request, b"{}\n"))
    monkeypatch.setattr(child, "build_capture_executor", lambda req, cfg: executor)
    monkeypatch.setattr(child, "_write_private_result", lambda _fd, data: written.append(data))

    assert child.run_capture_child("a" * 64, -1) == 0

    assert executor.calls == [(request, tmp_path)]
    result = CaptureResult.from_canonical_json(written[0])
    assert result.outcome == "success"
    assert result.size_bytes == 24


def test_child_serializes_categorized_failure_without_arbitrary_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    request = _request()
    failure = CategorizedError(
        "failed against qemu+tls://secret-host/system",
        category=ErrorCategory.TRANSPORT_FAILURE,
        terminal=True,
        details={
            "credential": "BEGIN PRIVATE KEY",  # pragma: allowlist secret
            "uri": "qemu+tls://secret-host/system",
        },
    )
    executor = _Executor(failure=failure)
    written: list[bytes] = []
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(child, "_open_attempt_directory", lambda: directory_fd)
    monkeypatch.setattr(child, "read_capture_inputs", lambda _fd: (request, b"{}\n"))
    monkeypatch.setattr(child, "build_capture_executor", lambda req, cfg: executor)
    monkeypatch.setattr(child, "_write_private_result", lambda _fd, data: written.append(data))

    assert child.run_capture_child("a" * 64, -1) == 0

    assert b"secret-host" not in written[0]
    assert b"PRIVATE KEY" not in written[0]
    result = CaptureResult.from_canonical_json(written[0])
    assert result.error_category is ErrorCategory.TRANSPORT_FAILURE
    assert result.terminal is True
    assert result.reason == "provider_execution_failed"
    assert result.details == {"phase": "provider_execution"}


def test_child_fails_closed_when_configuration_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    request = _request()
    written: list[bytes] = []
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(child, "_open_attempt_directory", lambda: directory_fd)
    monkeypatch.setattr(child, "read_capture_inputs", lambda _fd: (request, None))
    monkeypatch.setattr(child, "_write_private_result", lambda _fd, data: written.append(data))

    assert child.run_capture_child("a" * 64, -1) == 0

    result = CaptureResult.from_canonical_json(written[0])
    assert result.outcome == "failure"
    assert result.error_category is ErrorCategory.CONFIGURATION_ERROR
