"""Post-release child dispatch into provider executors (ADR-0558)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.capture_operations import child
from kdive.jobs.capture_operations import launcher as launcher_module
from kdive.jobs.capture_operations.launcher import GatedCaptureLauncher, LaunchedCapture
from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult
from kdive.jobs.capture_operations.repository import CaptureOperation
from kdive.providers.ports.traffic import (
    CaptureExecutionResult,
    LocalCaptureConfiguration,
    RemoteCaptureConfiguration,
)

_PCAP_HEADER = b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00" + b"\x00" * 16
_PRIVATE_PROVIDER_VALUE = b"qemu+tls://secret-host/system"
_PRIVATE_PROVIDER_DETAILS = b"provider-private-detail-marker"
_ROOT = Path(__file__).parents[3]
_MANIFEST_BUILDER = _ROOT / "scripts/build-capture-bootstrap-manifest.py"


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


def _operation(request: CaptureRequest) -> CaptureOperation:
    now = datetime.now(UTC)
    return CaptureOperation(
        id=uuid4(),
        job_id=request.job_id,
        job_attempt=1,
        worker_incarnation="provider-child-test",
        provider_kind=request.provider_kind,
        resource_id=request.resource_id,
        system_id=request.system_id,
        domain_name=request.domain_name,
        request_digest=request.digest,
        launch_token="a" * 64,
        host_instance="provider-child-host",
        boot_id=None,
        pid=None,
        start_ticks=None,
        state="launching",
        exit_outcome=None,
        exit_code=None,
        process_absent=False,
        provider_quiescence={},
        recovered_by=None,
        publication_state="pending",
        publication_object_key=None,
        publication_etag=None,
        publication_artifact_id=None,
        cleanup_capture_version_id=None,
        publication_tombstone_version=None,
        publication_started_at=None,
        publication_closed_at=None,
        spool_disposed_at=None,
        created_at=now,
        identity_recorded_at=None,
        running_at=None,
        cancel_requested_at=None,
        exited_at=None,
        updated_at=now,
    )


@pytest.fixture(scope="module")
def capture_manifest(tmp_path_factory: pytest.TempPathFactory) -> Path:
    destination = tmp_path_factory.mktemp("provider-child-manifest") / "manifest.json"
    subprocess.run(
        [
            sys.executable,
            str(_MANIFEST_BUILDER),
            "build",
            "--interpreter",
            sys.executable,
            "--source-root",
            str(_ROOT / "src"),
            "--output",
            str(destination),
        ],
        check=True,
    )
    return destination


def _write_overlay_package(path: Path, original: Path) -> None:
    path.mkdir(parents=True)
    (path / "__init__.py").write_text(
        '"""Test-owned package overlay for the gated provider child."""\n'
        f"__path__.append({str(original)!r})\n"
    )


def _carrier_source(class_name: str, *, remote: bool) -> str:
    source = textwrap.dedent(
        '''
        """Cross-process low-level provider fake loaded only by the test child."""

        from __future__ import annotations

        import json
        import time
        from pathlib import Path
        from uuid import UUID

        from kdive.domain.errors import CategorizedError, ErrorCategory

        _CONTROL = Path("provider-carrier.json")
        _CALLS = Path("provider-calls")
        _ENTERED = Path("provider-entered")
        _RELEASE = Path("provider-release")
        _PRIVATE = "qemu+tls://secret-host/system"


        def _settings() -> dict[str, object]:
            value = json.loads(_CONTROL.read_text())
            if not isinstance(value, dict):
                raise RuntimeError("provider carrier control must be an object")
            return value


        class __CLASS__:
            def __init__(self, **_kwargs: object) -> None:
                pass

            @property
            def write_remediation(self) -> str:
                return _PRIVATE

            def _call(self, name: str) -> None:
                with _CALLS.open("a", encoding="utf-8") as stream:
                    stream.write(f"{name}\\n")
                if name == "prepare":
                    _ENTERED.write_text("entered\\n")
                    _ENTERED.chmod(0o600)
                    while not _RELEASE.exists():
                        time.sleep(0.01)
                if _settings().get("failure") == name:
                    raise CategorizedError(
                        f"{name} failed against {_PRIVATE}",
                        category=ErrorCategory.TRANSPORT_FAILURE,
                        terminal=True,
                        details={"private": "provider-private-detail-marker"},
                    )

            def prepare(self, system_id: UUID, job_id: UUID) -> str:
                del system_id, job_id
                self._call("prepare")
                return "/provider/capture.pcap"

            def attach(
                self, domain_name: str, *, qom_id: str, dest_path: str, snaplen: int
            ) -> None:
                del domain_name, qom_id, dest_path, snaplen
                self._call("attach")

            def captured_size(self, dest_path: str) -> int:
                del dest_path
                self._call("captured_size")
                return int(_settings()["size"])

            def detach(self, domain_name: str, *, qom_id: str) -> None:
                del domain_name, qom_id
                self._call("detach")

            def fetch(self, dest_path: str, *, max_bytes: int) -> bytes:
                del dest_path, max_bytes
                self._call("fetch")
                return b"x" * int(_settings()["size"])

            def reclaim(self, dest_path: str) -> None:
                del dest_path
                self._call("reclaim")
        '''
    ).replace("__CLASS__", class_name)
    if remote:
        source += textwrap.dedent(
            """


            def open_libvirt_capture(*_args: object, **_kwargs: object) -> object:
                raise AssertionError("remote connection must remain below the fake carrier")
            """
        )
    return source


@pytest.fixture(scope="module")
def provider_overlay(tmp_path_factory: pytest.TempPathFactory) -> Path:
    overlay = tmp_path_factory.mktemp("provider-child-overlay")
    source = _ROOT / "src" / "kdive"
    _write_overlay_package(overlay / "kdive", source)
    _write_overlay_package(overlay / "kdive/providers", source / "providers")
    for provider, class_name in (
        ("local_libvirt", "LocalLibvirtTrafficCapture"),
        ("remote_libvirt", "RemoteLibvirtTrafficCapture"),
    ):
        provider_source = source / "providers" / provider
        _write_overlay_package(overlay / f"kdive/providers/{provider}", provider_source)
        _write_overlay_package(
            overlay / f"kdive/providers/{provider}/lifecycle",
            provider_source / "lifecycle",
        )
        (overlay / f"kdive/providers/{provider}/lifecycle/traffic_capture.py").write_text(
            _carrier_source(class_name, remote=provider == "remote_libvirt")
        )
    return overlay


async def _wait_for_provider_entry(child: LaunchedCapture) -> None:
    entered = child.attempt_dir / "provider-entered"
    for _ in range(500):
        if entered.exists():
            return
        if child.returncode is not None:
            raise AssertionError(
                f"provider child exited before entering carrier: {child.returncode}"
            )
        await asyncio.sleep(0.01)
    raise AssertionError("provider child did not enter the held carrier")


async def _observe_single_process_while_held(
    child: LaunchedCapture, stop: asyncio.Event, observed: asyncio.Event
) -> int:
    await _wait_for_provider_entry(child)
    samples = 0
    while not stop.is_set():
        task_root = Path(f"/proc/{child.identity.pid}/task")
        task_ids = {entry.name for entry in task_root.iterdir()}
        assert task_ids == {str(child.identity.pid)}
        children = task_root / str(child.identity.pid) / "children"
        assert children.read_text().strip() == ""
        assert child.returncode is None
        assert not (child.attempt_dir / "result.json").exists()
        assert not (child.attempt_dir / "provider-release").exists()
        samples += 1
        if samples >= 5:
            observed.set()
        await asyncio.sleep(0.005)
    return samples


async def _run_gated_provider_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: Path,
    overlay: Path,
    provider_kind: str,
    *,
    failure: str | None = None,
    size: int = len(_PCAP_HEADER),
) -> tuple[CaptureResult, bytes, Path, list[str], int]:
    request = _request(provider_kind)
    operation = _operation(request)
    monkeypatch.setattr(
        launcher_module,
        "__file__",
        str(overlay / "kdive/jobs/capture_operations/launcher.py"),
    )
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )
    launched = await launcher.launch(request, operation)
    release = launched.attempt_dir / "provider-release"
    observation: asyncio.Task[int] | None = None
    stop = asyncio.Event()
    observed = asyncio.Event()
    try:
        launched.stage_configuration(_configuration(request, launched.attempt_dir))
        configuration_path = launched.attempt_dir / "configuration.json"
        assert stat.S_IMODE(configuration_path.stat().st_mode) == 0o600
        _write_private(
            launched.attempt_dir / "provider-carrier.json",
            json.dumps({"failure": failure, "size": size}).encode(),
        )
        assert not (launched.attempt_dir / "provider-entered").exists()
        assert not (launched.attempt_dir / "result.json").exists()
        observation = asyncio.create_task(
            _observe_single_process_while_held(launched, stop, observed)
        )
        launched.release()
        await _wait_for_provider_entry(launched)
        await asyncio.wait_for(observed.wait(), timeout=2)
        stop.set()
        samples = await observation
        observation = None
        assert samples >= 5
        _write_private(release, b"release\n")
        result = await launched.wait()
        result_path = launched.attempt_dir / "result.json"
        result_bytes = result_path.read_bytes()
        calls = (launched.attempt_dir / "provider-calls").read_text().splitlines()
        return result, result_bytes, launched.attempt_dir, calls, samples
    finally:
        stop.set()
        if observation is not None:
            await asyncio.gather(observation, return_exceptions=True)
        if not release.exists():
            _write_private(release, b"release\n")
        await launched.cancel()


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
    capture_manifest: Path,
    provider_overlay: Path,
    provider_kind: str,
    method: str,
) -> None:
    result, result_bytes, attempt_dir, calls, samples = asyncio.run(
        _run_gated_provider_child(
            tmp_path,
            monkeypatch,
            capture_manifest,
            provider_overlay,
            provider_kind,
            failure=method,
        )
    )

    assert result.outcome == "failure"
    assert result.error_category is ErrorCategory.TRANSPORT_FAILURE
    assert result.terminal is True
    assert result.reason == "provider_execution_failed"
    assert result.details == {"phase": "provider_execution"}
    assert len(result_bytes) <= 65_536
    assert _PRIVATE_PROVIDER_VALUE not in result_bytes
    assert _PRIVATE_PROVIDER_DETAILS not in result_bytes
    assert samples >= 5
    assert stat.S_IMODE((attempt_dir / "result.json").stat().st_mode) == 0o600
    if method == "prepare":
        assert calls == ["prepare"]
    else:
        assert "detach" in calls
        assert "reclaim" in calls
    assert not (attempt_dir / "capture.pcap").exists()


@pytest.mark.parametrize("provider_kind", ["local-libvirt", "remote-libvirt"])
@pytest.mark.parametrize(
    ("size", "category"),
    [
        (5, ErrorCategory.CONFIGURATION_ERROR),
        (1_048_576 + 1, ErrorCategory.INFRASTRUCTURE_FAILURE),
    ],
    ids=["short", "oversized"],
)
def test_child_real_provider_dispatch_rejects_invalid_capture_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_manifest: Path,
    provider_overlay: Path,
    provider_kind: str,
    size: int,
    category: ErrorCategory,
) -> None:
    result, result_bytes, attempt_dir, calls, samples = asyncio.run(
        _run_gated_provider_child(
            tmp_path,
            monkeypatch,
            capture_manifest,
            provider_overlay,
            provider_kind,
            size=size,
        )
    )

    assert result.outcome == "failure"
    assert result.error_category is category
    assert result.details == {"phase": "provider_execution"}
    assert len(result_bytes) <= 65_536
    assert _PRIVATE_PROVIDER_VALUE not in result_bytes
    assert samples >= 5
    assert stat.S_IMODE((attempt_dir / "result.json").stat().st_mode) == 0o600
    assert "detach" in calls
    assert calls[-1] == "reclaim"
    assert not (attempt_dir / "capture.pcap").exists()


@pytest.mark.parametrize("provider_kind", ["local-libvirt", "remote-libvirt"])
def test_child_real_provider_dispatch_writes_bounded_success_without_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_manifest: Path,
    provider_overlay: Path,
    provider_kind: str,
) -> None:
    result, result_bytes, attempt_dir, calls, samples = asyncio.run(
        _run_gated_provider_child(
            tmp_path,
            monkeypatch,
            capture_manifest,
            provider_overlay,
            provider_kind,
        )
    )

    assert result.outcome == "success"
    assert result.size_bytes == len(_PCAP_HEADER)
    assert len(result_bytes) <= 65_536
    result_path = attempt_dir / "result.json"
    capture_path = attempt_dir / "capture.pcap"
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(capture_path.stat().st_mode) == 0o600
    assert capture_path.read_bytes() == b"x" * len(_PCAP_HEADER)
    assert calls[-3:] == ["detach", "fetch", "reclaim"]
    assert samples >= 5


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
