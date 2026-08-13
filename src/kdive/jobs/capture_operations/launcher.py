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

from kdive.jobs.capture_operations.bootstrap_elf import runtime_elf_closure
from kdive.jobs.capture_operations.linux_identity import LinuxIdentity, scan_launch_token
from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult
from kdive.jobs.capture_operations.repository import CaptureOperation

_DEFAULT_MANIFEST = Path("/usr/share/kdive/capture-bootstrap-manifest.json")
_HANDSHAKE_TIMEOUT_SECONDS = 5.0
_SIGNAL_WAIT_SECONDS = 5.0
_MAX_RESULT_BYTES = 65_536
_BOOTSTRAP_ENVIRONMENT = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TZ"})
_MAX_CONFIGURATION_BYTES = 16_384
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
_ELF_FINGERPRINT_KINDS = {
    "python-interpreter",
    "elf-interpreter",
    "elf-dependency",
    "bootstrap-extension",
}
_ELF_ROOT_KINDS = {"python-interpreter", "bootstrap-extension"}


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
    ordered_paths: list[str] = []
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
        ordered_paths.append(candidate)
    if ordered_paths != sorted(ordered_paths):
        raise RuntimeError("capture bootstrap manifest fingerprint paths are not sorted")
    return manifest


def _verified_manifest_paths(files: list[object]) -> list[tuple[str, Path, str]]:
    verified: list[tuple[str, Path, str]] = []
    for raw_entry in files:
        assert isinstance(raw_entry, dict)
        entry = cast(dict[str, object], raw_entry)
        kind = entry["kind"]
        expected = entry["sha256"]
        assert isinstance(kind, str) and isinstance(expected, str)
        candidate = Path(str(entry["path"]))
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise RuntimeError(
                f"capture bootstrap fingerprint path unavailable: {candidate}"
            ) from error
        if resolved != candidate:
            raise RuntimeError(f"capture bootstrap fingerprint path is a symlink: {candidate}")
        verified.append((kind, resolved, expected))
    return verified


def _verify_runtime_elf_paths(files: list[tuple[str, Path, str]]) -> None:
    expected = {path for kind, path, _digest in files if kind in _ELF_FINGERPRINT_KINDS}
    roots = [path for kind, path, _digest in files if kind in _ELF_ROOT_KINDS]
    closure, interpreters = runtime_elf_closure(roots, required_libraries=("libseccomp.so.2",))
    selected = closure | interpreters
    if selected != expected:
        raise RuntimeError("capture bootstrap runtime ELF closure drift")


def _read_manifest(path: Path, expected_uid: int) -> bytes:
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
    return raw


def _decode_manifest(raw: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("capture bootstrap manifest contains malformed JSON") from error
    return _validate_manifest_shape(decoded, raw)


def _verify_manifest_header(payload: Mapping[str, Any], interpreter: Path) -> None:
    architecture = _ARCHITECTURES.get(platform.machine().lower())
    if payload.get("schema_version") != 1:
        raise RuntimeError("capture bootstrap manifest has an unsupported schema")
    if payload.get("architecture") != architecture:
        raise RuntimeError("capture bootstrap manifest architecture drift")
    resolved_interpreter = interpreter.resolve(strict=True)
    if payload.get("interpreter") != str(resolved_interpreter):
        raise RuntimeError("capture bootstrap manifest interpreter drift")


def _verify_manifest_fingerprints(payload: Mapping[str, Any]) -> None:
    files = payload.get("files")
    assert isinstance(files, list)
    verified_files = _verified_manifest_paths(cast(list[object], files))
    _verify_runtime_elf_paths(verified_files)
    for _kind, candidate, expected in verified_files:
        actual = _sha256(candidate)
        if actual != expected:
            raise RuntimeError(f"capture bootstrap fingerprint drift: {candidate}")


def _verify_manifest_modules(payload: Mapping[str, Any]) -> None:
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


def _verify_manifest(path: Path, interpreter: Path, expected_uid: int) -> dict[str, Any]:
    payload = _decode_manifest(_read_manifest(path, expected_uid))
    _verify_manifest_header(payload, interpreter)
    _verify_manifest_fingerprints(payload)
    _verify_manifest_modules(payload)
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


class _ProcessMembershipChanged(ProcessLookupError):
    """A sampled process-group member vanished, moved, or reused its PID."""


def _parse_process_stat(pid: int, stat_line: str) -> tuple[int, int]:
    closing = stat_line.rfind(")")
    fields = stat_line[closing + 2 :].split() if closing > 1 else []
    if len(fields) <= 19:
        raise RuntimeError(f"malformed /proc/{pid}/stat during capture handoff")
    try:
        process_group = int(fields[2])
        start_ticks = int(fields[19])
    except ValueError as error:
        raise RuntimeError(f"malformed /proc/{pid}/stat during capture handoff") from error
    if start_ticks < 0:
        raise RuntimeError(f"malformed /proc/{pid}/stat during capture handoff")
    return process_group, start_ticks


def _identity_from_group_stat(
    pid: int,
    stat_line: str,
    *,
    process_group: int,
    host_instance: str,
) -> LinuxIdentity | None:
    observed_group, observed_start = _parse_process_stat(pid, stat_line)
    if observed_group != process_group:
        return None
    try:
        identity = LinuxIdentity.read(pid, host_instance=host_instance)
    except ProcessLookupError as error:
        raise _ProcessMembershipChanged(f"process-group member {pid} vanished") from error
    if identity.start_ticks != observed_start:
        raise _ProcessMembershipChanged(f"process-group member {pid} reused its identity")
    return identity


def _read_process_group_member(
    pid: int,
    *,
    process_group: int,
    host_instance: str,
    proc_root: Path = Path("/proc"),
) -> LinuxIdentity | None:
    try:
        stat_line = (proc_root / str(pid) / "stat").read_text()
    except FileNotFoundError as error:
        raise _ProcessMembershipChanged(f"process-group member {pid} vanished") from error
    except OSError as error:
        raise RuntimeError(f"cannot read /proc/{pid}/stat during capture handoff") from error
    return _identity_from_group_stat(
        pid,
        stat_line,
        process_group=process_group,
        host_instance=host_instance,
    )


def _process_group_members(
    process_group: int,
    proc_root: Path = Path("/proc"),
    *,
    host_instance: str,
) -> dict[int, LinuxIdentity]:
    members: dict[int, LinuxIdentity] = {}
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
        pid = int(entry.name)
        identity = _identity_from_group_stat(
            pid,
            stat_line,
            process_group=process_group,
            host_instance=host_instance,
        )
        if identity is not None:
            members[pid] = identity
    return members


def _task_members(pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    try:
        return {int(entry.name) for entry in (proc_root / str(pid) / "task").iterdir()}
    except OSError as error:
        raise RuntimeError(f"cannot enumerate /proc/{pid}/task during capture handoff") from error


type _ProcessHandle = tuple[LinuxIdentity, int]


def _attest_process_members(
    members: Mapping[int, LinuxIdentity],
    *,
    process_group: int,
    host_instance: str,
    existing: dict[int, _ProcessHandle],
) -> dict[int, _ProcessHandle]:
    """Open and revalidate every observed member before any member is signaled."""
    handles = dict(existing)
    opened: list[int] = []
    try:
        for pid, observed in sorted(members.items()):
            prior = handles.get(pid)
            if prior is not None:
                if prior[0] != observed:
                    raise _ProcessMembershipChanged(
                        f"process-group member {pid} reused its identity"
                    )
                continue
            try:
                pidfd = observed.open_pidfd(current_host_instance=host_instance)
                opened.append(pidfd)
                current = _read_process_group_member(
                    pid,
                    process_group=process_group,
                    host_instance=host_instance,
                )
            except ProcessLookupError as error:
                raise _ProcessMembershipChanged(
                    f"process-group member {pid} changed before cleanup"
                ) from error
            if current != observed:
                raise _ProcessMembershipChanged(
                    f"process-group member {pid} changed before cleanup"
                )
            handles[pid] = (observed, pidfd)
    except BaseException:
        for pidfd in opened:
            os.close(pidfd)
        raise
    return handles


async def _pidfd_ready(pidfd: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def _mark_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(pidfd, _mark_ready)
    try:
        await ready
    finally:
        loop.remove_reader(pidfd)


def _close_process_handles(handles: Mapping[int, _ProcessHandle]) -> None:
    for _identity, pidfd in handles.values():
        os.close(pidfd)


async def _signal_and_wait_exact(
    handles: dict[int, _ProcessHandle], process: asyncio.subprocess.Process
) -> None:
    """SIGKILL exact pidfds and await every member plus the leader for five seconds total."""
    for identity, pidfd in handles.values():
        with suppress(ProcessLookupError):
            identity.signal(pidfd, signal.SIGKILL)
    waits = [_pidfd_ready(pidfd) for _identity, pidfd in handles.values()]
    waits.append(process.wait())
    try:
        await asyncio.wait_for(asyncio.gather(*waits), timeout=_SIGNAL_WAIT_SECONDS)
    except TimeoutError as error:
        raise RuntimeError("capture launch cleanup exceeded 5 seconds") from error


async def _complete_token_scan(
    launch_token: str, *, interpreter: Path, host_instance: str
) -> tuple[LinuxIdentity, ...]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                scan_launch_token,
                launch_token,
                interpreter=interpreter,
                host_instance=host_instance,
            ),
            timeout=_SIGNAL_WAIT_SECONDS,
        )
    except TimeoutError as error:
        raise RuntimeError("complete launch-token scan exceeded 5 seconds") from error


async def _complete_process_group_scan(
    process_group: int,
    *,
    host_instance: str,
) -> dict[int, LinuxIdentity]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _process_group_members,
                process_group,
                host_instance=host_instance,
            ),
            timeout=_SIGNAL_WAIT_SECONDS,
        )
    except TimeoutError as error:
        raise RuntimeError("complete process-group scan exceeded 5 seconds") from error


def _acquire_token_handle(
    identity: LinuxIdentity,
    *,
    host_instance: str,
    handles: dict[int, _ProcessHandle],
    observed: Mapping[int, LinuxIdentity],
) -> int | None:
    prior_observation = observed.get(identity.pid)
    if prior_observation is not None and prior_observation != identity:
        raise _ProcessMembershipChanged(f"launch-token recovery observed reused pid {identity.pid}")
    prior = handles.get(identity.pid)
    if prior is not None and prior[0] == identity:
        return None
    if prior is not None:
        raise RuntimeError("launch-token recovery observed a reused process identity")
    try:
        pidfd = identity.open_pidfd(current_host_instance=host_instance)
    except ProcessLookupError:
        return None
    handles[identity.pid] = (identity, pidfd)
    return pidfd


async def _token_recovery_handles(
    launch_token: str,
    *,
    interpreter: Path,
    host_instance: str,
    existing: dict[int, _ProcessHandle],
    observed: Mapping[int, LinuxIdentity],
    matches: tuple[LinuxIdentity, ...] | None = None,
) -> dict[int, _ProcessHandle]:
    if matches is None:
        matches = await _complete_token_scan(
            launch_token,
            interpreter=interpreter,
            host_instance=host_instance,
        )
    handles = dict(existing)
    opened: list[int] = []
    try:
        for identity in matches:
            pidfd = _acquire_token_handle(
                identity,
                host_instance=host_instance,
                handles=handles,
                observed=observed,
            )
            if pidfd is not None:
                opened.append(pidfd)
    except BaseException:
        for pidfd in opened:
            os.close(pidfd)
        raise
    return handles


def _attest_observed_members(
    members: Mapping[int, LinuxIdentity] | None,
    *,
    process_group: int,
    host_instance: str,
    handles: dict[int, _ProcessHandle],
) -> tuple[dict[int, _ProcessHandle], bool]:
    if members is None:
        return handles, False
    try:
        return (
            _attest_process_members(
                members,
                process_group=process_group,
                host_instance=host_instance,
                existing=handles,
            ),
            False,
        )
    except _ProcessMembershipChanged:
        return handles, True


def _verify_recovered_members(
    recovered: Mapping[int, LinuxIdentity], observed: Mapping[int, LinuxIdentity]
) -> None:
    for pid, identity in recovered.items():
        prior_observation = observed.get(pid)
        if prior_observation is not None and prior_observation != identity:
            raise _ProcessMembershipChanged(f"process-group recovery observed reused pid {pid}")


async def _acquire_recovery_handles(
    process_group: int,
    *,
    launch_token: str,
    interpreter: Path,
    host_instance: str,
    handles: dict[int, _ProcessHandle],
    observed: Mapping[int, LinuxIdentity],
    recover_group: bool,
) -> dict[int, _ProcessHandle]:
    token_matches: tuple[LinuxIdentity, ...] | None = None
    if recover_group:
        recovered_members = await _complete_process_group_scan(
            process_group,
            host_instance=host_instance,
        )
        token_matches = await _complete_token_scan(
            launch_token,
            interpreter=interpreter,
            host_instance=host_instance,
        )
        _verify_recovered_members(recovered_members, observed)
        handles = _attest_process_members(
            recovered_members,
            process_group=process_group,
            host_instance=host_instance,
            existing=handles,
        )
    return await _token_recovery_handles(
        launch_token,
        interpreter=interpreter,
        host_instance=host_instance,
        existing=handles,
        observed=observed,
        matches=token_matches,
    )


async def _confirm_launch_absence(
    process_group: int,
    *,
    launch_token: str,
    interpreter: Path,
    host_instance: str,
    recover_group: bool,
) -> None:
    remaining = await _complete_token_scan(
        launch_token,
        interpreter=interpreter,
        host_instance=host_instance,
    )
    remaining_group: Mapping[int, LinuxIdentity] = {}
    if recover_group:
        remaining_group = await _complete_process_group_scan(
            process_group,
            host_instance=host_instance,
        )
    if remaining:
        raise RuntimeError("complete launch-token scan still finds capture children")
    if remaining_group:
        raise RuntimeError("complete process-group scan still finds capture children")


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
        if used_recovery:
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
        environment = self._child_environment()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=attempt_dir,
                env=environment,
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
            os.close(gate_write)
            leader = (identity, pidfd) if identity is not None and pidfd is not None else None
            try:
                await _cleanup_failed_launch(
                    process,
                    launch_token=operation.launch_token,
                    interpreter=interpreter,
                    host_instance=operation.host_instance,
                    leader=leader,
                    process_members=process_members,
                    force_recovery=isinstance(launch_error, _ProcessMembershipChanged),
                )
            except BaseException as cleanup_error:
                raise cleanup_error from launch_error
            raise
        assert identity is not None and pidfd is not None
        return LaunchedCapture(
            process=process,
            identity=identity,
            pidfd=pidfd,
            gate_fd=gate_write,
            attempt_dir=attempt_dir,
            argv=argv,
            environment=environment,
        )
