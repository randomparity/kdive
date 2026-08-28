"""Attested gated-process launcher and private capture spool (ADR-0558)."""

from __future__ import annotations

import asyncio
import os
import signal
import site
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from kdive.jobs.capture_operations.bootstrap.manifest_attestation import (
    verify_capture_bootstrap_manifest,
)
from kdive.jobs.capture_operations.process.linux_identity import LinuxIdentity
from kdive.jobs.capture_operations.process.linux_pidfd import require_pidfd_support
from kdive.jobs.capture_operations.process.process_fence import (
    _acquire_recovery_handles,
    _attest_observed_members,
    _close_process_handles,
    _confirm_launch_absence,
    _process_group_members,
    _ProcessHandle,
    _ProcessMembershipChanged,
    _signal_and_wait_exact,
    _task_members,
)
from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult
from kdive.jobs.capture_operations.storage.repository import CaptureOperation, LaunchAbortOutcome
from kdive.jobs.capture_operations.storage.spool import (
    _dispose_operation_spool,
    _read_capture,
    _read_result,
    _validate_private_directory,
    _write_configuration,
    _write_request,
)

_DEFAULT_MANIFEST = Path("/usr/share/kdive/capture-bootstrap-manifest.json")
_HANDSHAKE_TIMEOUT_SECONDS = 5.0
_SIGNAL_WAIT_SECONDS = 5.0
_BOOTSTRAP_ENVIRONMENT = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TZ"})


@dataclass(frozen=True, slots=True)
class LaunchAbortEvidence:
    """Complete cleanup facts for a launch that raised before identity became durable."""

    process_created: bool
    process_absent: bool
    provider_quiescence: Mapping[str, object]
    exit_outcome: LaunchAbortOutcome
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class _PreparedLaunch:
    interpreter: Path
    attempt_dir: Path
    gate_read: int
    gate_write: int
    argv: tuple[str, ...]
    environment: dict[str, str]


def _before_spawn_abort() -> LaunchAbortEvidence:
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


def _after_spawn_abort(
    operation: CaptureOperation, process: asyncio.subprocess.Process
) -> LaunchAbortEvidence:
    return LaunchAbortEvidence(
        process_created=True,
        process_absent=True,
        provider_quiescence={
            "evidence_kind": "closed_gate_boundary_token_scan_v1",
            "gate_closed": True,
            "boundary_scan_complete": True,
            "boundary_processes_absent": True,
            "host_instance": operation.host_instance,
            "launch_token": operation.launch_token,
            "launch_token_absent": True,
        },
        exit_outcome="aborted_before_identity",
        exit_code=process.returncode,
    )


async def _wait_failed_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=_SIGNAL_WAIT_SECONDS)
    except TimeoutError as error:
        raise RuntimeError("capture launch gate-EOF cleanup exceeded 5 seconds") from error


async def _cleanup_failed_launch(
    process: asyncio.subprocess.Process,
    *,
    launch_token: str,
    interpreter: Path,
    host_instance: str,
    leader: _ProcessHandle | None,
    process_members: Mapping[int, LinuxIdentity] | None,
    force_recovery: bool = False,
) -> None:
    """Boundedly terminate only exact attested members; never signal a numeric PID."""
    handles = {leader[0].pid: leader} if leader is not None else {}
    observed = dict(process_members or {})
    recover_group = force_recovery
    used_recovery = leader is None or recover_group
    try:
        handles, membership_changed = _attest_observed_members(
            process_members,
            process_group=process.pid,
            host_instance=host_instance,
            handles=handles,
        )
        recover_group = recover_group or membership_changed
        used_recovery = used_recovery or membership_changed
        if used_recovery:
            handles = await _acquire_recovery_handles(
                process.pid,
                launch_token=launch_token,
                interpreter=interpreter,
                host_instance=host_instance,
                handles=handles,
                observed=observed,
                recover_group=recover_group,
            )
        await _signal_and_wait_exact(handles, process)
        _close_process_handles(handles)
        handles = {}
        await _confirm_launch_absence(
            process.pid,
            launch_token=launch_token,
            interpreter=interpreter,
            host_instance=host_instance,
            recover_group=recover_group,
        )
    finally:
        if handles:
            _close_process_handles(handles)
        await _wait_failed_process(process)


@dataclass(slots=True)
class LaunchedCapture:
    """A filter-attested child stopped at its one-byte provider gate."""

    process: asyncio.subprocess.Process
    operation_id: UUID
    identity: LinuxIdentity
    pidfd: int
    gate_fd: int
    attempt_dir: Path
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    _released: bool = False
    _closed: bool = False
    _wait_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def returncode(self) -> int | None:
        """Expose the subprocess return code without weakening exact identity."""
        return self.process.returncode

    def release(self) -> None:
        """Write the sole release byte synchronously; callers can keep this await-free."""
        if self._released or self._closed:
            raise RuntimeError("capture gate is no longer releasable")
        written = os.write(self.gate_fd, b"R")
        if written != 1:
            raise RuntimeError("capture gate release write was incomplete")
        os.close(self.gate_fd)
        self._released = True
        self.gate_fd = -1

    def stage_configuration(self, configuration: bytes) -> None:
        """Stage opaque Task 3 configuration for access only after this gate releases."""
        if self._released or self._closed:
            raise RuntimeError("capture configuration gate is no longer writable")
        _write_configuration(self.attempt_dir, configuration)

    async def wait_process(self) -> int:
        """Wait for the exact launched process and close its retained pidfd once absent."""
        async with self._wait_lock:
            returncode = await self.process.wait()
            self._close_descriptors()
            return returncode

    async def wait(self) -> CaptureResult:
        """Wait for process exit, then parse the bounded no-follow result spool."""
        returncode = await self.wait_process()
        if returncode != 0 and not (self.attempt_dir / "result.json").exists():
            raise RuntimeError(f"capture child exited without a result: {returncode}")
        return self.read_result()

    def read_result(self) -> CaptureResult:
        """Read the bounded private result using a directory-relative no-follow open."""
        return _read_result(self.attempt_dir)

    def read_capture(self, maximum: int) -> bytes:
        """Read the private pcap only after the supervisor has acknowledged quiescence."""
        return _read_capture(self.attempt_dir, maximum)

    def dispose_spool(self) -> bool:
        """Remove and verify absence of only this operation's private mode-0700 spool."""
        return _dispose_operation_spool(self.attempt_dir, self.operation_id)

    async def _wait_bounded(self, seconds: float) -> bool:
        if self.process.returncode is not None:
            await self.wait_process()
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self.process.wait()), timeout=seconds)
        except TimeoutError:
            return False
        self._close_descriptors()
        return True

    async def cancel(self) -> bool:
        """Close an unreleased gate, else TERM/KILL the exact pidfd with two five-second waits."""
        if self.process.returncode is not None:
            await self.wait_process()
            return True
        if not self._released:
            if self.gate_fd >= 0:
                os.close(self.gate_fd)
                self.gate_fd = -1
            if await self._wait_bounded(_SIGNAL_WAIT_SECONDS):
                return True
        self.identity.signal(self.pidfd, signal.SIGTERM)
        if await self._wait_bounded(_SIGNAL_WAIT_SECONDS):
            return True
        self.identity.signal(self.pidfd, signal.SIGKILL)
        return await self._wait_bounded(_SIGNAL_WAIT_SECONDS)

    def _close_descriptors(self) -> None:
        if self._closed:
            return
        if self.gate_fd >= 0:
            os.close(self.gate_fd)
            self.gate_fd = -1
        os.close(self.pidfd)
        self._closed = True


class GatedCaptureLauncher:
    """Create an attested, private, descendant-free child stopped before provider input."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        manifest_path: Path = _DEFAULT_MANIFEST,
        interpreter: Path = Path(sys.executable),
        environment: Mapping[str, str] | None = None,
        expected_manifest_uid: int = 0,
    ) -> None:
        self._runtime_root = runtime_root
        self._manifest_path = manifest_path
        self._interpreter = interpreter
        self._environment = dict(os.environ if environment is None else environment)
        self._expected_manifest_uid = expected_manifest_uid

    def dispose_operation_spool(self, operation_id: UUID) -> bool:
        """Remove and verify the operation-derived private spool during recovery."""
        return _dispose_operation_spool(self._runtime_root / str(operation_id), operation_id)

    def _child_environment(self) -> dict[str, str]:
        environment = {
            key: value for key, value in self._environment.items() if key in _BOOTSTRAP_ENVIRONMENT
        }
        source_root = Path(__file__).parents[3]
        package_paths = [str(source_root), *site.getsitepackages()]
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(package_paths))
        environment["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
        environment.setdefault("LANG", "C.UTF-8")
        return environment

    def _attempt_directory(self, operation: CaptureOperation, request: CaptureRequest) -> Path:
        self._runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _validate_private_directory(self._runtime_root)
        root_fd = os.open(
            self._runtime_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            name = str(operation.id)
            os.mkdir(name, 0o700, dir_fd=root_fd)
            attempt_dir = self._runtime_root / name
            directory_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=root_fd
            )
            try:
                _write_request(directory_fd, request)
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)
        return attempt_dir

    @staticmethod
    def _validate_request(operation: CaptureOperation, request: CaptureRequest) -> None:
        matches = (
            operation.job_id == request.job_id
            and operation.provider_kind == request.provider_kind
            and operation.resource_id == request.resource_id
            and operation.system_id == request.system_id
            and operation.domain_name == request.domain_name
        )
        if not matches:
            raise ValueError("capture request identity does not match durable operation")
        if operation.request_digest != request.digest:
            raise ValueError("capture request digest does not match durable operation")
        if len(operation.launch_token) != 64 or any(
            character not in "0123456789abcdef" for character in operation.launch_token
        ):
            raise ValueError("durable launch token is not 256-bit lowercase hexadecimal")

    def _prepare_launch(
        self, request: CaptureRequest, operation: CaptureOperation
    ) -> _PreparedLaunch:
        require_pidfd_support()
        self._validate_request(operation, request)
        verify_capture_bootstrap_manifest(
            self._manifest_path,
            self._interpreter,
            expected_uid=self._expected_manifest_uid,
        )
        interpreter = self._interpreter.resolve(strict=True)
        attempt_dir = self._attempt_directory(operation, request)
        gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
        return _PreparedLaunch(
            interpreter=interpreter,
            attempt_dir=attempt_dir,
            gate_read=gate_read,
            gate_write=gate_write,
            argv=(
                str(interpreter),
                "-S",
                "-m",
                "kdive.jobs.capture_operations.bootstrap.bootstrap_entrypoint",
                "--launch-token",
                operation.launch_token,
                "--gate-fd",
                str(gate_read),
            ),
            environment=self._child_environment(),
        )

    async def launch(
        self,
        request: CaptureRequest,
        operation: CaptureOperation,
        *,
        on_abort: Callable[[LaunchAbortEvidence], None] | None = None,
    ) -> LaunchedCapture:
        """Spawn, attest, and return a child whose provider gate remains unreleased."""
        try:
            prepared = self._prepare_launch(request, operation)
        except BaseException:
            if on_abort is not None:
                on_abort(_before_spawn_abort())
            raise
        try:
            process = await asyncio.create_subprocess_exec(
                *prepared.argv,
                cwd=prepared.attempt_dir,
                env=prepared.environment,
                pass_fds=(prepared.gate_read,),
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except BaseException:
            os.close(prepared.gate_read)
            os.close(prepared.gate_write)
            if on_abort is not None:
                on_abort(_before_spawn_abort())
            raise
        os.close(prepared.gate_read)
        assert process.stdout is not None
        identity: LinuxIdentity | None = None
        pidfd: int | None = None
        process_members: dict[int, LinuxIdentity] | None = None
        try:
            handshake = await asyncio.wait_for(
                process.stdout.readexactly(1), timeout=_HANDSHAKE_TIMEOUT_SECONDS
            )
            if handshake != b"F":
                raise RuntimeError("capture child returned an invalid filter handshake")
            identity = LinuxIdentity.read(process.pid, host_instance=operation.host_instance)
            pidfd = identity.open_pidfd(current_host_instance=operation.host_instance)
            expected = {process.pid}
            for _ in range(2):
                process_members = _process_group_members(
                    process.pid,
                    host_instance=operation.host_instance,
                )
                task_members = _task_members(process.pid)
                if set(process_members) != expected:
                    raise RuntimeError("capture child process group was not empty at handoff")
                if task_members != expected:
                    raise RuntimeError("capture child task set was not empty at handoff")
                await asyncio.sleep(0)
        except BaseException as launch_error:
            os.close(prepared.gate_write)
            leader = (identity, pidfd) if identity is not None and pidfd is not None else None
            try:
                await _cleanup_failed_launch(
                    process,
                    launch_token=operation.launch_token,
                    interpreter=prepared.interpreter,
                    host_instance=operation.host_instance,
                    leader=leader,
                    process_members=process_members,
                    force_recovery=isinstance(launch_error, _ProcessMembershipChanged),
                )
            except BaseException as cleanup_error:
                raise cleanup_error from launch_error
            if on_abort is not None:
                on_abort(_after_spawn_abort(operation, process))
            raise
        assert identity is not None and pidfd is not None
        return LaunchedCapture(
            process=process,
            operation_id=operation.id,
            identity=identity,
            pidfd=pidfd,
            gate_fd=prepared.gate_write,
            attempt_dir=prepared.attempt_dir,
            argv=prepared.argv,
            environment=prepared.environment,
        )
