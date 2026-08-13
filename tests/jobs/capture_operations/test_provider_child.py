"""Post-release child dispatch into provider executors (ADR-0558)."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.capture_operations import child
from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult
from kdive.providers.local_libvirt import composition as local_composition
from kdive.providers.local_libvirt.lifecycle import capture_operation as local_capture_operation
from kdive.providers.ports.traffic import (
    CaptureExecutionResult,
    LocalCaptureConfiguration,
    RemoteCaptureConfiguration,
)
from kdive.providers.remote_libvirt import composition as remote_composition
from kdive.providers.remote_libvirt.lifecycle import capture_operation as remote_capture_operation

_PCAP_HEADER = b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00" + b"\x00" * 16
_PRIVATE_PROVIDER_VALUE = b"qemu+tls://secret-host/system"


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


class _ProviderCarrier:
    """Low-level provider fake beneath the real dispatch and concrete executor."""

    def __init__(self, *, failure: str | None = None, data: bytes = _PCAP_HEADER) -> None:
        self.failure = failure
        self.data = data
        self.calls: list[str] = []

    @property
    def write_remediation(self) -> str:
        return _PRIVATE_PROVIDER_VALUE.decode()

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.failure == name:
            raise CategorizedError(
                f"{name} failed against {_PRIVATE_PROVIDER_VALUE.decode()}",
                category=ErrorCategory.TRANSPORT_FAILURE,
                terminal=True,
                details={"credential": "BEGIN PRIVATE KEY"},  # pragma: allowlist secret
            )

    def prepare(self, system_id: UUID, job_id: UUID) -> str:
        del system_id, job_id
        self._call("prepare")
        return "/provider/capture.pcap"

    def attach(self, domain_name: str, *, qom_id: str, dest_path: str, snaplen: int) -> None:
        del domain_name, qom_id, dest_path, snaplen
        self._call("attach")

    def captured_size(self, dest_path: str) -> int:
        del dest_path
        self._call("captured_size")
        return len(self.data)

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        del domain_name, qom_id
        self._call("detach")

    def fetch(self, dest_path: str, *, max_bytes: int) -> bytes:
        del dest_path, max_bytes
        self._call("fetch")
        return self.data

    def reclaim(self, dest_path: str) -> None:
        del dest_path
        self._call("reclaim")


def _configuration(request: CaptureRequest, tmp_path: Path) -> bytes:
    if request.provider_kind == "local-libvirt":
        return LocalCaptureConfiguration(
            resource_id=request.resource_id,
            uri="qemu:///session",
        ).to_canonical_json()
    return RemoteCaptureConfiguration(
        resource_id=request.resource_id,
        uri="qemu+tls://remote.example/system",
        client_cert_ref="client.crt",
        client_key_ref="client.key",  # pragma: allowlist secret
        ca_cert_ref="ca.crt",
        secrets_root=str(tmp_path),
        storage_pool="default",
    ).to_canonical_json()


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _run_real_provider_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
    carrier: _ProviderCarrier,
) -> tuple[CaptureResult, bytes, str, str]:
    request = _request(provider_kind)
    tmp_path.chmod(0o700)
    request_bytes = request.to_canonical_json()
    _write_private(tmp_path / "request.json", request_bytes)
    _write_private(tmp_path / "request.sha256", f"{request.digest}\n".encode())
    _write_private(tmp_path / "configuration.json", _configuration(request, tmp_path))
    monkeypatch.chdir(tmp_path)

    if provider_kind == "local-libvirt":
        monkeypatch.setattr(
            local_composition,
            "LocalLibvirtTrafficCapture",
            lambda **_kwargs: carrier,
        )
        monkeypatch.setattr(local_capture_operation, "_POLL_INTERVAL_SECONDS", 0)
    else:
        monkeypatch.setattr(
            remote_composition,
            "RemoteLibvirtTrafficCapture",
            lambda **_kwargs: carrier,
        )
        monkeypatch.setattr(remote_capture_operation, "_POLL_INTERVAL_SECONDS", 0)

    pid = Path("/proc/self").resolve().name
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    before = children_path.read_text()
    assert child.run_capture_child("a" * 64, -1) == 0
    after = children_path.read_text()
    result_bytes = (tmp_path / "result.json").read_bytes()
    return CaptureResult.from_canonical_json(result_bytes), result_bytes, before, after


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


@pytest.mark.parametrize("provider_kind", ["local-libvirt", "remote-libvirt"])
@pytest.mark.parametrize(
    "method", ["prepare", "attach", "captured_size", "detach", "fetch", "reclaim"]
)
def test_child_runs_each_real_provider_dispatch_failure_after_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
    method: str,
) -> None:
    carrier = _ProviderCarrier(failure=method)

    result, result_bytes, children_before, children_after = _run_real_provider_child(
        tmp_path, monkeypatch, provider_kind, carrier
    )

    assert result.outcome == "failure"
    assert result.error_category is ErrorCategory.TRANSPORT_FAILURE
    assert result.terminal is True
    assert result.reason == "provider_execution_failed"
    assert result.details == {"phase": "provider_execution"}
    assert len(result_bytes) <= 65_536
    assert _PRIVATE_PROVIDER_VALUE not in result_bytes
    assert b"PRIVATE KEY" not in result_bytes
    assert children_after == children_before
    if method != "prepare":
        assert "reclaim" in carrier.calls
    assert not (tmp_path / "capture.pcap").exists()


@pytest.mark.parametrize("provider_kind", ["local-libvirt", "remote-libvirt"])
@pytest.mark.parametrize(
    ("data", "category"),
    [
        (b"short", ErrorCategory.CONFIGURATION_ERROR),
        (b"x" * (1_048_576 + 1), ErrorCategory.INFRASTRUCTURE_FAILURE),
    ],
    ids=["short", "oversized"],
)
def test_child_real_provider_dispatch_rejects_unbounded_capture_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
    data: bytes,
    category: ErrorCategory,
) -> None:
    carrier = _ProviderCarrier(data=data)

    result, result_bytes, children_before, children_after = _run_real_provider_child(
        tmp_path, monkeypatch, provider_kind, carrier
    )

    assert result.outcome == "failure"
    assert result.error_category is category
    assert result.details == {"phase": "provider_execution"}
    assert len(result_bytes) <= 65_536
    assert _PRIVATE_PROVIDER_VALUE not in result_bytes
    assert children_after == children_before
    assert "detach" in carrier.calls
    assert carrier.calls[-1] == "reclaim"
    assert not (tmp_path / "capture.pcap").exists()


@pytest.mark.parametrize("provider_kind", ["local-libvirt", "remote-libvirt"])
def test_child_real_provider_dispatch_writes_bounded_success_without_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
) -> None:
    carrier = _ProviderCarrier()

    result, result_bytes, children_before, children_after = _run_real_provider_child(
        tmp_path, monkeypatch, provider_kind, carrier
    )

    assert result.outcome == "success"
    assert result.size_bytes == len(_PCAP_HEADER)
    assert len(result_bytes) <= 65_536
    assert (tmp_path / "capture.pcap").read_bytes() == _PCAP_HEADER
    assert carrier.calls[-3:] == ["detach", "fetch", "reclaim"]
    assert children_after == children_before


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
