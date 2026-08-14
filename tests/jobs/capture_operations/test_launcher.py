"""Real-process and fault tests for the gated child launcher (ADR-0558)."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.jobs.capture_operations import launcher as launcher_module
from kdive.jobs.capture_operations import linux_identity as linux_identity_module
from kdive.jobs.capture_operations.launcher import (
    GatedCaptureLauncher,
    LaunchAbortEvidence,
    LaunchedCapture,
)
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.jobs.capture_operations.repository import CaptureOperation
from kdive.providers.ports.traffic import LocalCaptureConfiguration

_ROOT = Path(__file__).parents[3]
_MANIFEST_BUILDER = _ROOT / "scripts/build-capture-bootstrap-manifest.py"


def _request() -> CaptureRequest:
    return CaptureRequest(
        job_id=uuid4(),
        provider_kind="local-libvirt",
        resource_id=uuid4(),
        system_id=uuid4(),
        domain_name="kdive-test-domain",
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=1,
    )


def _operation(request: CaptureRequest) -> CaptureOperation:
    now = datetime.now(UTC)
    return CaptureOperation(
        id=uuid4(),
        job_id=request.job_id,
        job_attempt=1,
        worker_incarnation="test-worker",
        provider_kind=request.provider_kind,
        resource_id=request.resource_id,
        system_id=request.system_id,
        domain_name=request.domain_name,
        request_digest=request.digest,
        launch_token="a" * 64,
        host_instance="test-host",
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


def _before_spawn_evidence() -> LaunchAbortEvidence:
    return LaunchAbortEvidence(
        process_created=False,
        process_absent=True,
        provider_quiescence={
            "evidence_kind": "spawn_not_created_v1",
            "process_created": False,
        },
        exit_outcome="aborted_before_spawn",
        exit_code=None,
    )


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    destination = tmp_path / "capture-bootstrap-manifest.json"
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


def test_real_child_is_gated_and_has_exact_process_contract(
    tmp_path: Path, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    operation = _operation(request)
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://forbidden")
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///forbidden-before-release")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_MACHINE", "forbidden-before-release")
    monkeypatch.setenv("LD_PRELOAD", "/forbidden/loader.so")
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _run() -> None:
        child = await launcher.launch(request, operation)
        try:
            assert not (child.attempt_dir / "result.json").exists()
            assert os.stat(child.attempt_dir).st_mode & 0o777 == 0o700
            assert os.stat(child.attempt_dir / "request.json").st_mode & 0o777 == 0o600
            assert os.stat(child.attempt_dir / "request.sha256").st_mode & 0o777 == 0o600

            argv = (Path(f"/proc/{child.identity.pid}/cmdline").read_bytes()).split(b"\0")[:-1]
            assert argv == [
                os.fsencode(str(Path(sys.executable).resolve())),
                b"-S",
                b"-m",
                b"kdive.capture_bootstrap",
                b"--launch-token",
                b"a" * 64,
                b"--gate-fd",
                child.argv[-1].encode(),
            ]
            assert not hasattr(child, "child_gate_fd")
            assert Path(f"/proc/{child.identity.pid}/cwd").resolve() == child.attempt_dir.resolve()
            environ = Path(f"/proc/{child.identity.pid}/environ").read_bytes().split(b"\0")
            assert not any(item.startswith(b"KDIVE_DATABASE_URL=") for item in environ)
            assert not any(item.startswith(b"KDIVE_LIBVIRT_URI=") for item in environ)
            assert not any(item.startswith(b"KDIVE_REMOTE_LIBVIRT_MACHINE=") for item in environ)
            assert not any(item.startswith(b"LD_PRELOAD=") for item in environ)
            assert set(
                launcher_module._process_group_members(
                    child.identity.pid,
                    host_instance=operation.host_instance,
                )
            ) == {child.identity.pid}
            children = Path(
                f"/proc/{child.identity.pid}/task/{child.identity.pid}/children"
            ).read_text()
            assert children.strip() == ""

            child.release()
            result = await child.wait()
            assert result.reason == "provider_execution_failed"
            assert (child.attempt_dir / "result.json").exists()
        finally:
            await child.cancel()

    asyncio.run(_run())


def test_real_launch_uses_libc_when_python_omits_pidfd_wrappers(
    tmp_path: Path, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(linux_identity_module.linux_pidfd.os, "pidfd_open", raising=False)
    monkeypatch.delattr(
        linux_identity_module.linux_pidfd.signal, "pidfd_send_signal", raising=False
    )
    request = _request()
    operation = _operation(request)
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _run() -> None:
        child = await launcher.launch(request, operation)
        await child.cancel()

    asyncio.run(_run())


def test_provider_configuration_uses_post_filter_spool_seam(tmp_path: Path, manifest: Path) -> None:
    request = _request()
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _run() -> None:
        child = await launcher.launch(request, _operation(request))
        configuration = LocalCaptureConfiguration(
            resource_id=request.resource_id,
            uri="qemu:///system",
        ).to_canonical_json()
        assert not (child.attempt_dir / "configuration.json").exists()
        child.stage_configuration(configuration)
        configuration_path = child.attempt_dir / "configuration.json"
        assert configuration_path.read_bytes() == configuration
        assert configuration_path.stat().st_mode & 0o777 == 0o600
        child.release()
        assert (await child.wait()).reason == "provider_execution_failed"

    asyncio.run(_run())


def test_gate_eof_exits_without_opening_request(tmp_path: Path, manifest: Path) -> None:
    request = _request()
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _run() -> None:
        child = await launcher.launch(request, _operation(request))
        os.chmod(child.attempt_dir / "request.json", 0)
        assert await child.cancel()
        assert child.returncode == 0
        assert not (child.attempt_dir / "result.json").exists()

    asyncio.run(_run())


def test_request_digest_mismatch_fails_before_spawn(tmp_path: Path, manifest: Path) -> None:
    request = _request()
    operation = _operation(request)
    operation = replace(operation, request_digest="0" * 64)
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    with pytest.raises(ValueError, match="request digest"):
        asyncio.run(launcher.launch(request, operation))
    assert not (tmp_path / "runtime" / str(operation.id)).exists()


def test_spool_refuses_symlink_attempt_directory(tmp_path: Path, manifest: Path) -> None:
    request = _request()
    operation = _operation(request)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / str(operation.id)).symlink_to(tmp_path)
    launcher = GatedCaptureLauncher(
        runtime_root=runtime,
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    with pytest.raises(FileExistsError):
        asyncio.run(launcher.launch(request, operation))


def test_manifest_mode_and_fingerprint_drift_fail_before_spawn(
    tmp_path: Path, manifest: Path
) -> None:
    request = _request()
    operation = _operation(request)
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )
    aborts: list[LaunchAbortEvidence] = []
    os.chmod(manifest, 0o600)
    with pytest.raises(PermissionError, match="0644"):
        asyncio.run(launcher.launch(request, operation, on_abort=aborts.append))
    assert aborts == [_before_spawn_evidence()]

    os.chmod(manifest, 0o644)
    data = manifest.read_text()
    manifest.write_text(data.replace('"sha256":"', '"sha256":"0', 1))
    with pytest.raises(RuntimeError, match="fingerprint"):
        asyncio.run(launcher.launch(request, operation, on_abort=aborts.append))
    assert aborts == [_before_spawn_evidence(), _before_spawn_evidence()]


def test_result_reader_rejects_symlink_and_oversize(tmp_path: Path, manifest: Path) -> None:
    request = _request()
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _run() -> None:
        child = await launcher.launch(request, _operation(request))
        child.release()
        await child.wait_process()
        result = child.attempt_dir / "result.json"
        result.unlink()
        target = tmp_path / "large.json"
        target.write_bytes(b"x" * 65_537)
        result.symlink_to(target)
        with pytest.raises(OSError):
            child.read_result()

    asyncio.run(_run())


def test_capture_reader_is_private_bounded_and_no_follow(tmp_path: Path, manifest: Path) -> None:
    request = _request()
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _run() -> None:
        child = await launcher.launch(request, _operation(request))
        child.release()
        await child.wait_process()
        capture = child.attempt_dir / "capture.pcap"
        capture.write_bytes(b"pcap")
        capture.chmod(0o600)
        assert child.read_capture(4) == b"pcap"
        with pytest.raises(ValueError, match="exceeds 3 bytes"):
            child.read_capture(3)
        capture.unlink()
        target = tmp_path / "outside.pcap"
        target.write_bytes(b"pcap")
        capture.symlink_to(target)
        with pytest.raises(OSError):
            child.read_capture(4)

    asyncio.run(_run())


@pytest.mark.parametrize("payload", [b"x" * 65_537, b"{not-json\n"])
def test_result_reader_rejects_oversize_and_malformed_json(
    tmp_path: Path, manifest: Path, payload: bytes
) -> None:
    request = _request()
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _run() -> None:
        child = await launcher.launch(request, _operation(request))
        child.release()
        await child.wait_process()
        result = child.attempt_dir / "result.json"
        result.write_bytes(payload)
        os.chmod(result, 0o600)
        with pytest.raises(ValueError):
            child.read_result()

    asyncio.run(_run())


def test_identity_write_failure_keeps_provider_gate_closed(tmp_path: Path, manifest: Path) -> None:
    request = _request()
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _run() -> None:
        child = await launcher.launch(request, _operation(request))
        try:
            raise RuntimeError("identity-write fault")
        except RuntimeError:
            assert await child.cancel()
        assert not (child.attempt_dir / "result.json").exists()

    asyncio.run(_run())


def test_child_rejects_request_digest_drift_after_release(tmp_path: Path, manifest: Path) -> None:
    request = _request()
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _run() -> None:
        child = await launcher.launch(request, _operation(request))
        changed = request.model_copy(update={"domain_name": "changed-after-launch"})
        (child.attempt_dir / "request.json").write_bytes(changed.to_canonical_json())
        os.chmod(child.attempt_dir / "request.json", 0o600)
        child.release()
        assert await child.wait_process() != 0
        assert not (child.attempt_dir / "result.json").exists()

    asyncio.run(_run())


def test_spawn_failure_closes_gate_and_never_releases(
    tmp_path: Path, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    operation = _operation(request)
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )

    async def _fail_spawn(*_args: object, **_kwargs: object) -> None:
        raise OSError("spawn fault")

    aborts: list[LaunchAbortEvidence] = []
    monkeypatch.setattr(launcher_module.asyncio, "create_subprocess_exec", _fail_spawn)
    with pytest.raises(OSError, match="spawn fault"):
        asyncio.run(launcher.launch(request, operation, on_abort=aborts.append))
    assert not (tmp_path / "runtime" / str(operation.id) / "result.json").exists()
    assert len(aborts) == 1
    assert aborts[0].process_created is False
    assert aborts[0].process_absent is True
    assert aborts[0].exit_outcome == "aborted_before_spawn"
    assert aborts[0].provider_quiescence == {
        "evidence_kind": "spawn_not_created_v1",
        "process_created": False,
    }


@pytest.mark.parametrize("fault", ["stat", "pidfd", "process-group"])
def test_post_spawn_attestation_faults_abort_before_release(
    tmp_path: Path, manifest: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    request = _request()
    operation = _operation(request)
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )
    if fault == "stat":
        original_read = launcher_module.LinuxIdentity.read
        read_calls = 0

        def _fail_first_read(pid: int, *, host_instance: str) -> object:
            nonlocal read_calls
            read_calls += 1
            if read_calls == 1:
                raise RuntimeError(f"stat fault on {host_instance}")
            return original_read(pid, host_instance=host_instance)

        monkeypatch.setattr(
            launcher_module.LinuxIdentity,
            "read",
            _fail_first_read,
        )
    elif fault == "pidfd":
        original_open_pidfd = launcher_module.LinuxIdentity.open_pidfd
        pidfd_calls = 0

        def _fail_first_pidfd(
            identity: launcher_module.LinuxIdentity, *, current_host_instance: str
        ) -> int:
            nonlocal pidfd_calls
            pidfd_calls += 1
            if pidfd_calls == 1:
                raise OSError(f"pidfd fault on {current_host_instance}")
            return original_open_pidfd(identity, current_host_instance=current_host_instance)

        monkeypatch.setattr(
            launcher_module.LinuxIdentity,
            "open_pidfd",
            _fail_first_pidfd,
        )
    else:
        group_scans = 0

        def _extra_member(pid: int, *, host_instance: str) -> dict[int, object]:
            nonlocal group_scans
            group_scans += 1
            if group_scans == 3:
                return {}
            identity = launcher_module.LinuxIdentity.read(pid, host_instance=host_instance)
            if group_scans == 1:
                impossible = replace(identity, pid=2_147_483_647)
                return {pid: identity, impossible.pid: impossible}
            return {pid: identity}

        monkeypatch.setattr(launcher_module, "_process_group_members", _extra_member)

    aborts: list[LaunchAbortEvidence] = []
    with pytest.raises((OSError, RuntimeError), match=fault.replace("-", " ")):
        asyncio.run(launcher.launch(request, operation, on_abort=aborts.append))
    assert not (tmp_path / "runtime" / str(operation.id) / "result.json").exists()
    assert len(aborts) == 1
    abort = aborts[0]
    assert abort.process_created is True
    assert abort.process_absent is True
    assert abort.exit_outcome == "aborted_before_identity"
    assert abort.provider_quiescence == {
        "evidence_kind": "closed_gate_boundary_token_scan_v1",
        "gate_closed": True,
        "boundary_scan_complete": True,
        "boundary_processes_absent": True,
        "host_instance": operation.host_instance,
        "launch_token": operation.launch_token,
        "launch_token_absent": True,
    }


def test_stale_post_spawn_numeric_identity_never_signals_unrelated_group(
    tmp_path: Path, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    operation = _operation(request)
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    unrelated_pidfd = linux_identity_module.linux_pidfd.open_pidfd(unrelated.pid)
    numeric_signals: list[tuple[str, int, int]] = []
    scan_calls: list[str] = []

    class _StaleIdentity:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        @classmethod
        def read(cls, pid: int, *, host_instance: str) -> _StaleIdentity:
            del host_instance
            return cls(pid)

        def open_pidfd(self, *, current_host_instance: str) -> int:
            del current_host_instance
            raise ProcessLookupError("leader numeric identity became stale")

    def _scan(*args: object, **kwargs: object) -> tuple[linux_identity_module.LinuxIdentity, ...]:
        scan_calls.append("scan")
        return linux_identity_module.scan_launch_token(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(launcher_module, "LinuxIdentity", _StaleIdentity)
    monkeypatch.setattr(launcher_module, "scan_launch_token", _scan, raising=False)
    monkeypatch.setattr(
        launcher_module.os,
        "killpg",
        lambda pid, sig: numeric_signals.append(("killpg", pid, sig)),
    )
    monkeypatch.setattr(
        launcher_module.os,
        "kill",
        lambda pid, sig: numeric_signals.append(("kill", pid, sig)),
    )
    try:
        with pytest.raises(ProcessLookupError, match="stale"):
            asyncio.run(launcher.launch(request, operation))
        assert scan_calls
        assert numeric_signals == []
        assert unrelated.poll() is None
    finally:
        linux_identity_module.linux_pidfd.send_signal(unrelated_pidfd, signal.SIGKILL)
        os.close(unrelated_pidfd)
        unrelated.wait(timeout=5)


def test_exact_process_members_are_all_attested_before_any_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []

    class _Identity:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def open_pidfd(self, *, current_host_instance: str) -> int:
            assert current_host_instance == "host-a"
            events.append(("open", self.pid))
            return os.open(os.devnull, os.O_RDONLY)

        def signal(self, _pidfd: int, _sig: int) -> None:
            events.append(("signal", self.pid))

    leader = _Identity(10)
    members = {pid: _Identity(pid) for pid in (10, 20, 30)}
    members[10] = leader
    leader_fd = os.open(os.devnull, os.O_RDONLY)
    monkeypatch.setattr(launcher_module, "LinuxIdentity", _Identity)
    monkeypatch.setattr(
        launcher_module,
        "_read_process_group_member",
        lambda pid, **_kwargs: events.append(("revalidate", pid)) or members[pid],
    )
    handles = launcher_module._attest_process_members(
        members,  # ty: ignore[invalid-argument-type] - identity fakes
        process_group=10,
        host_instance="host-a",
        existing={10: (leader, leader_fd)},  # ty: ignore[invalid-argument-type] - identity fake
    )
    try:
        for identity, pidfd in handles.values():
            identity.signal(pidfd, signal.SIGKILL)
        first_signal = next(index for index, event in enumerate(events) if event[0] == "signal")
        assert events[:first_signal] == [
            ("open", 20),
            ("revalidate", 20),
            ("open", 30),
            ("revalidate", 30),
        ]
        assert events[first_signal:] == [("signal", 10), ("signal", 20), ("signal", 30)]
    finally:
        launcher_module._close_process_handles(handles)


def test_extra_member_pid_reuse_never_signals_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signaled: list[tuple[int, str]] = []
    group_scans: list[str] = []
    token_scans: list[str] = []

    def _ready_pidfd() -> int:
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        return read_fd

    class _Identity:
        def __init__(self, pid: int, generation: str) -> None:
            self.pid = pid
            self.generation = generation

        @classmethod
        def read(cls, pid: int, *, host_instance: str) -> _Identity:
            assert host_instance == "host-a"
            return replacement if pid == 20 else leader

        def open_pidfd(self, *, current_host_instance: str) -> int:
            assert current_host_instance == "host-a"
            return _ready_pidfd()

        def signal(self, _pidfd: int, _sig: int) -> None:
            signaled.append((self.pid, self.generation))

    class _Process:
        pid = 10
        returncode = 0

        async def wait(self) -> int:
            return 0

    leader = _Identity(10, "leader")
    observed = _Identity(20, "observed")
    replacement = _Identity(20, "replacement")

    def _group_scan(*_args: object, **_kwargs: object) -> dict[int, _Identity]:
        group_scans.append("group")
        return {10: leader, 20: replacement}

    def _token_scan(*_args: object, **_kwargs: object) -> tuple[()]:
        token_scans.append("token")
        return ()

    monkeypatch.setattr(launcher_module, "LinuxIdentity", _Identity)
    monkeypatch.setattr(launcher_module, "_process_group_members", _group_scan)
    monkeypatch.setattr(
        launcher_module,
        "_read_process_group_member",
        lambda *_args, **_kwargs: replacement,
        raising=False,
    )
    monkeypatch.setattr(launcher_module, "scan_launch_token", _token_scan)

    with pytest.raises(ProcessLookupError, match="reused"):
        asyncio.run(
            launcher_module._cleanup_failed_launch(
                _Process(),  # ty: ignore[invalid-argument-type] - exact cleanup process fake
                launch_token="a" * 64,
                interpreter=Path(sys.executable),
                host_instance="host-a",
                leader=(leader, _ready_pidfd()),  # ty: ignore[invalid-argument-type] - identity fake
                process_members={10: leader, 20: observed},  # ty: ignore[invalid-argument-type]
            )
        )

    assert signaled == []
    assert group_scans == ["group"]
    assert token_scans == ["token"]


def test_process_group_recovery_scan_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Identity:
        def __init__(self, pid: int, generation: str) -> None:
            self.pid = pid
            self.generation = generation

        def open_pidfd(self, *, current_host_instance: str) -> int:
            assert current_host_instance == "host-a"
            read_fd, write_fd = os.pipe()
            os.close(write_fd)
            return read_fd

        def signal(self, _pidfd: int, _sig: int) -> None:
            pass

    class _Process:
        pid = 10
        returncode = 0

        async def wait(self) -> int:
            return 0

    leader = _Identity(10, "leader")
    observed = _Identity(20, "observed")
    replacement = _Identity(20, "replacement")

    def _slow_group_scan(*_args: object, **_kwargs: object) -> dict[int, _Identity]:
        time.sleep(0.05)
        return {10: leader}

    monkeypatch.setattr(launcher_module, "_SIGNAL_WAIT_SECONDS", 0.001)
    monkeypatch.setattr(launcher_module, "_process_group_members", _slow_group_scan)
    monkeypatch.setattr(
        launcher_module,
        "_read_process_group_member",
        lambda *_args, **_kwargs: replacement,
    )
    monkeypatch.setattr(launcher_module, "scan_launch_token", lambda *_args, **_kwargs: ())

    with pytest.raises(RuntimeError, match="process-group scan exceeded"):
        asyncio.run(
            launcher_module._cleanup_failed_launch(
                _Process(),  # ty: ignore[invalid-argument-type] - exact cleanup process fake
                launch_token="a" * 64,
                interpreter=Path(sys.executable),
                host_instance="host-a",
                leader=(leader, leader.open_pidfd(current_host_instance="host-a")),  # ty: ignore[invalid-argument-type]
                process_members={10: leader, 20: observed},  # ty: ignore[invalid-argument-type]
            )
        )


def test_recovery_acquisition_failure_closes_each_owned_pidfd_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []

    class _Identity:
        def __init__(self, pid: int, pidfd: int | None) -> None:
            self.pid = pid
            self.pidfd = pidfd

        def open_pidfd(self, *, current_host_instance: str) -> int:
            assert current_host_instance == "host-a"
            if self.pidfd is None:
                raise RuntimeError("token pidfd fault")
            return self.pidfd

        def signal(self, _pidfd: int, _sig: int) -> None:
            raise AssertionError("acquisition failure must precede signaling")

    class _Process:
        pid = 10
        returncode = 0

        async def wait(self) -> int:
            return 0

    leader = _Identity(10, 101)
    recovered = _Identity(20, 202)
    token_failure = _Identity(30, None)

    async def _group_scan(*_args: object, **_kwargs: object) -> dict[int, _Identity]:
        return {10: leader, 20: recovered}

    async def _token_scan(*_args: object, **_kwargs: object) -> tuple[_Identity, ...]:
        return (token_failure,)

    monkeypatch.setattr(launcher_module, "_complete_process_group_scan", _group_scan)
    monkeypatch.setattr(launcher_module, "_complete_token_scan", _token_scan)
    monkeypatch.setattr(
        launcher_module,
        "_read_process_group_member",
        lambda pid, **_kwargs: recovered if pid == 20 else leader,
    )
    monkeypatch.setattr(
        launcher_module,
        "_close_process_handles",
        lambda handles: closed.extend(pidfd for _identity, pidfd in handles.values()),
    )

    with pytest.raises(RuntimeError, match="token pidfd fault"):
        asyncio.run(
            launcher_module._cleanup_failed_launch(
                _Process(),  # ty: ignore[invalid-argument-type] - exact cleanup process fake
                launch_token="a" * 64,
                interpreter=Path(sys.executable),
                host_instance="host-a",
                leader=(leader, 101),  # ty: ignore[invalid-argument-type] - identity fake
                process_members={10: leader},  # ty: ignore[invalid-argument-type]
                force_recovery=True,
            )
        )

    assert closed == [202, 101]


def test_unreadable_token_recovery_scan_fails_closed_without_numeric_signal(
    tmp_path: Path, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    operation = _operation(request)
    launcher = GatedCaptureLauncher(
        runtime_root=tmp_path / "runtime",
        manifest_path=manifest,
        interpreter=Path(sys.executable),
        expected_manifest_uid=os.getuid(),
    )
    numeric_signals: list[tuple[int, int]] = []

    class _UnavailableIdentity:
        @classmethod
        def read(cls, _pid: int, *, host_instance: str) -> _UnavailableIdentity:
            del host_instance
            return cls()

        def open_pidfd(self, *, current_host_instance: str) -> int:
            del current_host_instance
            raise ProcessLookupError("identity unavailable")

    monkeypatch.setattr(launcher_module, "LinuxIdentity", _UnavailableIdentity)
    monkeypatch.setattr(
        launcher_module,
        "scan_launch_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unreadable token scan")),
    )
    monkeypatch.setattr(
        launcher_module.os,
        "killpg",
        lambda pid, sig: numeric_signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        launcher_module.os,
        "kill",
        lambda pid, sig: numeric_signals.append((pid, sig)),
    )

    with pytest.raises(RuntimeError, match="unreadable token scan"):
        asyncio.run(launcher.launch(request, operation))
    assert numeric_signals == []


def test_release_write_fault_does_not_mark_gate_released(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    child = LaunchedCapture(
        process=object(),  # ty: ignore[invalid-argument-type] - release never reads it
        operation_id=uuid4(),
        identity=object(),  # ty: ignore[invalid-argument-type] - release never reads it
        pidfd=-1,
        gate_fd=write_fd,
        attempt_dir=Path("/unused"),
        argv=(),
        environment={},
    )
    monkeypatch.setattr(launcher_module.os, "write", lambda _fd, _data: 0)
    try:
        with pytest.raises(RuntimeError, match="incomplete"):
            child.release()
        assert not child._released
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_cancel_uses_term_then_kill_and_preserves_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []

    class _Identity:
        def signal(self, _pidfd: int, sig: int) -> None:
            signals.append(sig)

    class _Process:
        returncode = None

    child = LaunchedCapture(
        process=_Process(),  # ty: ignore[invalid-argument-type] - narrow cancellation fake
        operation_id=uuid4(),
        identity=_Identity(),  # ty: ignore[invalid-argument-type] - narrow cancellation fake
        pidfd=99,
        gate_fd=-1,
        attempt_dir=Path("/unused"),
        argv=(),
        environment={},
        _released=True,
    )
    waits = iter((False, False))

    async def _timeout(_self: LaunchedCapture, seconds: float) -> bool:
        assert seconds == 5.0
        return next(waits)

    monkeypatch.setattr(LaunchedCapture, "_wait_bounded", _timeout)
    assert not asyncio.run(child.cancel())
    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_dispose_spool_removes_only_exact_private_operation_directory(tmp_path: Path) -> None:
    operation_id = uuid4()
    attempt_dir = tmp_path / str(operation_id)
    attempt_dir.mkdir(mode=0o700)
    files = ("request.json", "request.sha256", "configuration.json", "result.json", "capture.pcap")
    for name in files:
        path = attempt_dir / name
        path.write_bytes(b"data")
        path.chmod(0o600)
    sibling = tmp_path / "sibling"
    sibling.write_text("keep", encoding="utf-8")
    child = LaunchedCapture(
        process=object(),  # ty: ignore[invalid-argument-type] - disposal never reads it
        operation_id=operation_id,
        identity=object(),  # ty: ignore[invalid-argument-type] - disposal never reads it
        pidfd=-1,
        gate_fd=-1,
        attempt_dir=attempt_dir,
        argv=(),
        environment={},
    )

    assert child.dispose_spool()
    assert not attempt_dir.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert child.dispose_spool()


def test_dispose_spool_refuses_unexpected_or_non_private_entries(tmp_path: Path) -> None:
    operation_id = uuid4()
    attempt_dir = tmp_path / str(operation_id)
    attempt_dir.mkdir(mode=0o700)
    unexpected = attempt_dir / "foreign"
    unexpected.write_bytes(b"keep")
    unexpected.chmod(0o600)
    child = LaunchedCapture(
        process=object(),  # ty: ignore[invalid-argument-type] - disposal never reads it
        operation_id=operation_id,
        identity=object(),  # ty: ignore[invalid-argument-type] - disposal never reads it
        pidfd=-1,
        gate_fd=-1,
        attempt_dir=attempt_dir,
        argv=(),
        environment={},
    )

    assert not child.dispose_spool()
    assert unexpected.read_bytes() == b"keep"


def test_dispose_spool_accepts_an_absent_private_root(tmp_path: Path) -> None:
    operation_id = uuid4()
    child = LaunchedCapture(
        process=object(),  # ty: ignore[invalid-argument-type] - disposal never reads it
        operation_id=operation_id,
        identity=object(),  # ty: ignore[invalid-argument-type] - disposal never reads it
        pidfd=-1,
        gate_fd=-1,
        attempt_dir=tmp_path / "absent-root" / str(operation_id),
        argv=(),
        environment={},
    )

    assert child.dispose_spool()


def test_process_group_enumeration_fails_closed_on_unreadable_proc(tmp_path: Path) -> None:
    process = tmp_path / "123"
    process.mkdir()
    stat_path = process / "stat"
    stat_path.write_text("123 (capture) S 1 123 1")
    stat_path.chmod(0)
    try:
        with pytest.raises(RuntimeError, match="cannot read"):
            launcher_module._process_group_members(
                123,
                tmp_path,
                host_instance="host-a",
            )
    finally:
        stat_path.chmod(0o600)


def test_process_group_enumeration_skips_pid_vanishing_during_stat_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = tmp_path / "123"
    process.mkdir()
    stat_path = process / "stat"
    stat_path.write_text("123 (capture) S 1 123 1")
    original_read_text = Path.read_text

    def _read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == stat_path:
            raise ProcessLookupError("process vanished after stat was opened")
        return original_read_text(path, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(Path, "read_text", _read_text)

    assert (
        launcher_module._process_group_members(
            123,
            tmp_path,
            host_instance="host-a",
        )
        == {}
    )


def test_process_group_enumeration_binds_observed_start_ticks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = tmp_path / "123"
    process.mkdir()
    fields = ["S", "1", "123", *(["0"] * 16), "42"]
    (process / "stat").write_text(f"123 (capture) {' '.join(fields)}\n")
    replacement = linux_identity_module.LinuxIdentity(
        host_instance="host-a",
        boot_id="boot-a",
        pid=123,
        start_ticks=99,
    )
    monkeypatch.setattr(
        launcher_module.LinuxIdentity,
        "read",
        lambda _pid, *, host_instance: replacement,
    )

    with pytest.raises(ProcessLookupError, match="reused"):
        launcher_module._process_group_members(
            123,
            tmp_path,
            host_instance="host-a",
        )
