"""Attested gated-process launcher and private capture spool (ADR-0558)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import signal
import site
import stat
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from kdive.jobs.capture_operations.linux_identity import LinuxIdentity
from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult
from kdive.jobs.capture_operations.repository import CaptureOperation

_DEFAULT_MANIFEST = Path("/usr/share/kdive/capture-bootstrap-manifest.json")
_HANDSHAKE_TIMEOUT_SECONDS = 5.0
_SIGNAL_WAIT_SECONDS = 5.0
_MAX_RESULT_BYTES = 65_536
_PROVIDER_ENVIRONMENT = {
    "local-libvirt": frozenset({"KDIVE_LIBVIRT_URI"}),
    "remote-libvirt": frozenset(
        {
            "KDIVE_REMOTE_LIBVIRT_STORAGE_POOL",
            "KDIVE_REMOTE_LIBVIRT_NETWORK",
            "KDIVE_REMOTE_LIBVIRT_MACHINE",
        }
    ),
}
_COMMON_ENVIRONMENT = frozenset(
    {"LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}
)
_ARCHITECTURES = {"amd64": "x86_64", "x86_64": "x86_64", "ppc64le": "ppc64le"}
_MANIFEST_KEYS = {
    "schema_version",
    "architecture",
    "interpreter",
    "bootstrap_modules",
    "files",
}
_FINGERPRINT_KINDS = {
    "python-interpreter",
    "elf-interpreter",
    "elf-dependency",
    "bootstrap-python",
    "bootstrap-extension",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_manifest_shape(payload: object, raw: bytes) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_KEYS:
        raise RuntimeError("capture bootstrap manifest has an unsupported schema")
    manifest = cast(dict[str, Any], payload)
    canonical = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if raw != canonical:
        raise RuntimeError("capture bootstrap manifest is not canonical JSON")
    modules = manifest.get("bootstrap_modules")
    files = manifest.get("files")
    module_names = cast(list[str], modules) if isinstance(modules, list) else []
    if (
        not isinstance(modules, list)
        or not all(isinstance(module, str) for module in modules)
        or module_names != sorted(set(module_names))
        or not isinstance(files, list)
        or not files
    ):
        raise RuntimeError("capture bootstrap manifest has malformed trace data")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"kind", "path", "sha256"}:
            raise RuntimeError("capture bootstrap manifest fingerprint entry is malformed")
        fingerprint = cast(dict[str, Any], entry)
        candidate = fingerprint["path"]
        digest = fingerprint["sha256"]
        if (
            fingerprint["kind"] not in _FINGERPRINT_KINDS
            or not isinstance(candidate, str)
            or not candidate.startswith("/")
            or candidate in seen
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError("capture bootstrap manifest fingerprint entry is malformed")
        seen.add(candidate)
    return manifest


def _verify_manifest(path: Path, interpreter: Path, expected_uid: int) -> dict[str, Any]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise PermissionError("capture bootstrap manifest must be a regular file")
    if metadata.st_uid != expected_uid:
        raise PermissionError("capture bootstrap manifest has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) != 0o644:
        raise PermissionError("capture bootstrap manifest must have mode 0644")
    raw = path.read_bytes()
    if len(raw) > 1_048_576:
        raise RuntimeError("capture bootstrap manifest exceeds 1048576 bytes")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("capture bootstrap manifest contains malformed JSON") from error
    payload = _validate_manifest_shape(decoded, raw)
    architecture = _ARCHITECTURES.get(platform.machine().lower())
    if payload.get("schema_version") != 1:
        raise RuntimeError("capture bootstrap manifest has an unsupported schema")
    if payload.get("architecture") != architecture:
        raise RuntimeError("capture bootstrap manifest architecture drift")
    resolved_interpreter = interpreter.resolve(strict=True)
    if payload.get("interpreter") != str(resolved_interpreter):
        raise RuntimeError("capture bootstrap manifest interpreter drift")
    files = payload.get("files")
    assert isinstance(files, list)
    for entry in files:
        assert isinstance(entry, dict)
        candidate = Path(str(entry.get("path", "")))
        expected = entry.get("sha256")
        assert isinstance(expected, str)
        try:
            resolved = candidate.resolve(strict=True)
            if resolved != candidate:
                raise RuntimeError(f"capture bootstrap fingerprint path is a symlink: {candidate}")
            actual = _sha256(resolved)
        except OSError as error:
            raise RuntimeError(
                f"capture bootstrap fingerprint path unavailable: {candidate}"
            ) from error
        if actual != expected:
            raise RuntimeError(f"capture bootstrap fingerprint drift: {candidate}")
    modules = payload.get("bootstrap_modules")
    required = {
        "kdive",
        "kdive.jobs",
        "kdive.jobs.capture_operations",
        "kdive.jobs.capture_operations.sandbox",
        "kdive.capture_bootstrap",
    }
    assert isinstance(modules, list)
    if not required.issubset(modules):
        raise RuntimeError("capture bootstrap import-trace drift")
    return payload


def verify_capture_bootstrap_manifest(
    manifest_path: Path = _DEFAULT_MANIFEST,
    interpreter: Path = Path(sys.executable),
    *,
    expected_uid: int = 0,
) -> None:
    """Fail readiness when the root/image-owned bootstrap attestation is stale or malformed."""
    _verify_manifest(manifest_path, interpreter, expected_uid)


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


def _process_group_members(process_group: int, proc_root: Path = Path("/proc")) -> set[int]:
    members: set[int] = set()
    try:
        entries = list(proc_root.iterdir())
    except OSError as error:
        raise RuntimeError("cannot enumerate /proc for capture child handoff") from error
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            stat_line = (entry / "stat").read_text()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(f"cannot read {entry}/stat during capture handoff") from error
        closing = stat_line.rfind(")")
        fields = stat_line[closing + 2 :].split() if closing > 1 else []
        if len(fields) < 3:
            raise RuntimeError(f"malformed {entry}/stat during capture handoff")
        if int(fields[2]) == process_group:
            members.add(int(entry.name))
    return members


def _task_members(pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    try:
        return {int(entry.name) for entry in (proc_root / str(pid) / "task").iterdir()}
    except OSError as error:
        raise RuntimeError(f"cannot enumerate /proc/{pid}/task during capture handoff") from error


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


@dataclass(slots=True)
class LaunchedCapture:
    """A filter-attested child stopped at its one-byte provider gate."""

    process: asyncio.subprocess.Process
    identity: LinuxIdentity
    pidfd: int
    gate_fd: int
    child_gate_fd: int
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

    def _child_environment(self, provider_kind: str) -> dict[str, str]:
        allowed = _COMMON_ENVIRONMENT | _PROVIDER_ENVIRONMENT[provider_kind]
        environment = {key: value for key, value in self._environment.items() if key in allowed}
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

    async def launch(self, request: CaptureRequest, operation: CaptureOperation) -> LaunchedCapture:
        """Spawn, attest, and return a child whose provider gate remains unreleased."""
        self._validate_request(operation, request)
        verify_capture_bootstrap_manifest(
            self._manifest_path,
            self._interpreter,
            expected_uid=self._expected_manifest_uid,
        )
        interpreter = self._interpreter.resolve(strict=True)
        attempt_dir = self._attempt_directory(operation, request)
        gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
        argv = (
            str(interpreter),
            "-S",
            "-m",
            "kdive.capture_bootstrap",
            "--launch-token",
            operation.launch_token,
            "--gate-fd",
            str(gate_read),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=attempt_dir,
                env=self._child_environment(request.provider_kind),
                pass_fds=(gate_read,),
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except BaseException:
            os.close(gate_read)
            os.close(gate_write)
            raise
        os.close(gate_read)
        assert process.stdout is not None
        pidfd: int | None = None
        try:
            handshake = await asyncio.wait_for(
                process.stdout.readexactly(1), timeout=_HANDSHAKE_TIMEOUT_SECONDS
            )
            if handshake != b"F":
                raise RuntimeError("capture child returned an invalid filter handshake")
            identity = LinuxIdentity.read(process.pid)
            pidfd = identity.open_pidfd()
            expected = {process.pid}
            for _ in range(2):
                if _process_group_members(process.pid) != expected:
                    raise RuntimeError("capture child process group was not empty at handoff")
                if _task_members(process.pid) != expected:
                    raise RuntimeError("capture child task set was not empty at handoff")
                await asyncio.sleep(0)
        except BaseException:
            os.close(gate_write)
            if pidfd is not None:
                os.close(pidfd)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise
        return LaunchedCapture(
            process=process,
            identity=identity,
            pidfd=pidfd,
            gate_fd=gate_write,
            child_gate_fd=gate_read,
            attempt_dir=attempt_dir,
            argv=argv,
            environment=self._child_environment(request.provider_kind),
        )
