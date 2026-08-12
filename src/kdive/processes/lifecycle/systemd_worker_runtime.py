"""Exact, bounded systemd evidence and control for fixed host worker slots."""

from __future__ import annotations

import math
import os
import posixpath
import pwd
import re
import selectors
import shlex
import stat
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from kdive.processes.lifecycle.systemd_worker_state import _SLOTS_MODE

type CgroupMembership = Literal["populated", "empty", "unknown"]

_UNIT = re.compile(r"kdive-live-worker@[1-8]\.service")
_WORKER_TEMPLATE_SLICE = r"/system.slice/system-kdive\x2dlive\x2dworker.slice"
_FIXED_WORKER_CGROUP = re.compile(
    re.escape(_WORKER_TEMPLATE_SLICE.encode("ascii")) + rb"/kdive-live-worker@[1-8]\.service"
)
_PYTHON_LAUNCHERS = frozenset((b"python", b"python3", b"python3.14"))
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SYSTEMD_VALUE = re.compile(r"[A-Za-z0-9_.@:-]+")
_PROPERTIES = (
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "ControlGroup",
    "InvocationID",
)
_PROPERTY_ARGUMENT = "--property=" + ",".join(_PROPERTIES)
_DIAGNOSTIC_PROPERTIES = ("ActiveState", "SubState", "Result", "ExecMainStatus")
_CONTROL_OUTPUT_LIMIT = 4096
_CGROUP_EVENTS_LIMIT = 4096
_PROC_FILE_LIMIT = 4096
_MAX_JOURNAL_BYTES = 320 * 1024
_MAX_DIAGNOSTIC_ENVIRONMENT_BYTES = 64 * 1024
_MAX_DIAGNOSTIC_CREDENTIAL_BYTES = 4096
_SECRET_ENVIRONMENT_NAMES = frozenset(
    ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "KDIVE_DATABASE_URL")
)
_SECRET_ENVIRONMENT_PATTERN = re.compile(r"(?i)(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|DATABASE_URL)")
_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_COMMAND_CLEANUP_RESERVE_SECONDS = 0.5
_COMMAND_TERMINATE_GRACE_SECONDS = 0.1


class SystemdUnavailable(RuntimeError):
    """The system manager or one required observation cannot be read exactly."""


class SystemdConflict(RuntimeError):
    """Observed host facts contradict the fixed worker-unit contract."""


class CommandDeadlineExceeded(SystemdUnavailable):
    """A child command did not finish inside the shared monotonic deadline."""


class CommandCleanupDeadlineExceeded(CommandDeadlineExceeded):
    """A child could not be terminated and reaped by the shared deadline."""


class CommandOutputTooLarge(SystemdConflict):
    """A child command exceeded its pre-decode output ceiling."""


class Deadline(Protocol):
    """One shared monotonic deadline view."""

    def remaining(self) -> float:
        """Return non-negative seconds left on the shared operation deadline."""
        ...


@dataclass(frozen=True, slots=True)
class _ExecutionDeadline:
    """Leave bounded termination and reap time inside the caller deadline."""

    parent: Deadline

    def remaining(self) -> float:
        return max(0.0, self.parent.remaining() - _COMMAND_CLEANUP_RESERVE_SECONDS)


@dataclass(frozen=True, slots=True)
class MonotonicDeadline:
    """An absolute deadline measured only against an injected monotonic clock."""

    expires_at: float
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.expires_at):
            raise ValueError("deadline expiry must be finite")

    @classmethod
    def after(
        cls, seconds: float, *, monotonic: Callable[[], float] = time.monotonic
    ) -> MonotonicDeadline:
        """Create a deadline ``seconds`` from one reading of ``monotonic``."""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("deadline duration must be finite non-negative seconds")
        return cls(expires_at=monotonic() + seconds, monotonic=monotonic)

    def remaining(self) -> float:
        """Return the remaining seconds without exposing a negative timeout."""
        return max(0.0, self.expires_at - self.monotonic())


class CommandRunner(Protocol):
    """Injected argument-array command boundary."""

    def run(
        self,
        argv: Sequence[str],
        *,
        byte_limit: int,
        deadline: Deadline | None = None,
        allow_truncation: bool = False,
    ) -> str:
        """Run one bounded child and decode only its retained output bytes."""
        ...


class SubprocessCommandRunner:
    """Run argument arrays under one shared deadline with bounded merged output."""

    def __init__(self, deadline: Deadline) -> None:
        self.deadline = deadline

    def run(
        self,
        argv: Sequence[str],
        *,
        byte_limit: int,
        deadline: Deadline | None = None,
        allow_truncation: bool = False,
    ) -> str:
        """Run one child without a shell, terminating it on timeout or output exhaustion."""
        if not argv or any(not isinstance(argument, str) or not argument for argument in argv):
            raise ValueError("command requires a non-empty argument array")
        if byte_limit <= 0:
            raise ValueError("command byte limit must be positive")
        operation_deadline = deadline or self.deadline
        execution_deadline = _ExecutionDeadline(operation_deadline)
        if execution_deadline.remaining() <= 0:
            raise CommandDeadlineExceeded("command deadline elapsed before child launch")
        process = self._launch(argv)
        output, truncated = self._collect_with_cleanup(
            process,
            byte_limit,
            execution_deadline,
            operation_deadline,
        )
        if truncated:
            self._terminate(process, operation_deadline)
        if truncated and not allow_truncation:
            raise CommandOutputTooLarge(f"{argv[0]} output exceeded {byte_limit} bytes")
        if not truncated and process.returncode != 0:
            raise SystemdUnavailable(f"{argv[0]} exited with status {process.returncode}")
        return output.decode("utf-8", errors="replace")

    @staticmethod
    def _launch(argv: Sequence[str]) -> subprocess.Popen[bytes]:
        try:
            return subprocess.Popen(  # noqa: S603 - argv is fixed or validated by the caller.
                tuple(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
            )
        except OSError as exc:
            raise SystemdUnavailable(f"failed to launch {argv[0]}") from exc

    def _collect_with_cleanup(
        self,
        process: subprocess.Popen[bytes],
        byte_limit: int,
        execution_deadline: Deadline,
        cleanup_deadline: Deadline,
    ) -> tuple[bytes, bool]:
        try:
            return self._collect(process, byte_limit, execution_deadline)
        except BaseException as exc:
            try:
                self._terminate(process, cleanup_deadline)
            except CommandCleanupDeadlineExceeded as cleanup_exc:
                raise cleanup_exc from exc
            raise

    def _collect(
        self, process: subprocess.Popen[bytes], byte_limit: int, deadline: Deadline
    ) -> tuple[bytes, bool]:
        if process.stdout is None:
            raise RuntimeError("bounded command child has no stdout pipe")
        output = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                remaining = deadline.remaining()
                if remaining <= 0:
                    raise CommandDeadlineExceeded("child command exceeded its monotonic deadline")
                events = selector.select(remaining)
                if not events:
                    raise CommandDeadlineExceeded("child command exceeded its monotonic deadline")
                chunk = os.read(process.stdout.fileno(), byte_limit + 1 - len(output))
                if not chunk:
                    selector.unregister(process.stdout)
                    continue
                output.extend(chunk)
                if len(output) > byte_limit:
                    return bytes(output[:byte_limit]), True
            self._wait(process, deadline)
            return bytes(output), False
        finally:
            selector.close()
            process.stdout.close()

    @staticmethod
    def _wait(process: subprocess.Popen[bytes], deadline: Deadline) -> None:
        remaining = deadline.remaining()
        if remaining <= 0:
            raise CommandDeadlineExceeded("child command exceeded its monotonic deadline")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise CommandDeadlineExceeded("child command exceeded its monotonic deadline") from exc

    @classmethod
    def _terminate(cls, process: subprocess.Popen[bytes], deadline: Deadline) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError as exc:
            raise CommandCleanupDeadlineExceeded(
                "child command could not receive SIGTERM before its deadline"
            ) from exc
        if cls._wait_for_cleanup(
            process,
            deadline,
            ceiling=_COMMAND_TERMINATE_GRACE_SECONDS,
        ):
            return
        try:
            process.kill()
        except OSError as exc:
            raise CommandCleanupDeadlineExceeded(
                "child command could not receive SIGKILL before its deadline"
            ) from exc
        if not cls._wait_for_cleanup(process, deadline):
            raise CommandCleanupDeadlineExceeded(
                "child command could not be reaped before its monotonic deadline"
            )

    @staticmethod
    def _wait_for_cleanup(
        process: subprocess.Popen[bytes], deadline: Deadline, *, ceiling: float | None = None
    ) -> bool:
        remaining = deadline.remaining()
        if remaining <= 0:
            return False
        timeout = remaining if ceiling is None else min(remaining, ceiling)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True


@dataclass(frozen=True, slots=True)
class UnitObservation:
    """Exact systemd and recursive-cgroup facts for one fixed unit invocation."""

    unit: str
    boot_id: str
    invocation_id: str
    active_state: str
    sub_state: str
    result: str
    exec_main_status: int
    control_group: str
    membership: CgroupMembership


@dataclass(frozen=True, slots=True)
class BootObservation:
    """Current boot evidence when one fixed unit has no active invocation identity."""

    unit: str
    boot_id: str


type SystemdObservation = UnitObservation | BootObservation


@dataclass(frozen=True, slots=True, order=True)
class UnmanagedWorker:
    """A live ``kdive worker`` process outside every fixed worker unit cgroup."""

    pid: int
    uid: int


def load_slot_redaction_values(
    root: Path,
    slot: int,
    *,
    expected_uid: int = 0,
    expected_gid: int | None = None,
) -> tuple[str, ...]:
    """Read only trusted secret sources for one fixed retained slot."""
    if slot not in range(1, 9):
        raise ValueError("diagnostic slot must be in 1..8")
    descriptors: list[int] = []
    try:
        gid = pwd.getpwnam(f"kdive-worker-{slot}").pw_gid if expected_gid is None else expected_gid
        descriptors, parent = _open_diagnostic_slot(root, slot, expected_uid, gid)
        credential = _read_diagnostic_source(
            parent,
            "worker-incarnation.credential",
            mode=0o400,
            maximum=_MAX_DIAGNOSTIC_CREDENTIAL_BYTES,
            expected_uid=expected_uid,
            expected_gid=gid,
        )
        environment = _read_diagnostic_source(
            parent,
            "worker.env",
            mode=0o600,
            maximum=_MAX_DIAGNOSTIC_ENVIRONMENT_BYTES,
            expected_uid=expected_uid,
            expected_gid=gid,
        )
    except (KeyError, OSError, UnicodeError, ValueError) as exc:
        raise PermissionError("slot diagnostic redaction source is unsafe") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    try:
        credential_text = credential.decode("utf-8")
        secret_values = _secret_environment_values(environment)
    except (UnicodeError, ValueError) as exc:
        raise PermissionError("slot diagnostic redaction source is unsafe") from exc
    if not credential_text or any(character in credential_text for character in "\r\n\x00"):
        raise PermissionError("slot diagnostic redaction source is unsafe")
    return (credential_text, *secret_values)


def _open_diagnostic_slot(
    root: Path, slot: int, expected_uid: int, slot_gid: int
) -> tuple[list[int], int]:
    descriptors: list[int] = []
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        descriptors.append(parent)
        _validate_diagnostic_directory(
            parent, mode=0o755, expected_uid=expected_uid, expected_gid=expected_uid
        )
        for name, mode, gid in (
            ("slots", _SLOTS_MODE, expected_uid),
            (str(slot), 0o750, slot_gid),
        ):
            parent = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            descriptors.append(parent)
            _validate_diagnostic_directory(
                parent, mode=mode, expected_uid=expected_uid, expected_gid=gid
            )
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    return descriptors, parent


def _validate_diagnostic_directory(
    descriptor: int,
    *,
    mode: int,
    expected_uid: int,
    expected_gid: int,
) -> None:
    metadata = os.fstat(descriptor)
    trusted = (
        stat.S_ISDIR(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == mode
        and metadata.st_uid == expected_uid
        and metadata.st_gid == expected_gid
        and metadata.st_nlink >= 2
    )
    if not trusted:
        raise PermissionError("slot diagnostic redaction source is unsafe")


def _read_diagnostic_source(
    parent: int,
    name: str,
    *,
    mode: int,
    maximum: int,
    expected_uid: int,
    expected_gid: int,
) -> bytes:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent,
    )
    try:
        before = os.fstat(descriptor)
        if not _trusted_diagnostic_source(
            before,
            mode=mode,
            maximum=maximum,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        ):
            raise PermissionError("slot diagnostic redaction source is unsafe")
        data = _read_exact_diagnostic_source(descriptor, before.st_size)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(data) != before.st_size or not _same_diagnostic_source(before, after):
        raise PermissionError("slot diagnostic redaction source is unsafe")
    return data


def _trusted_diagnostic_source(
    metadata: os.stat_result,
    *,
    mode: int,
    maximum: int,
    expected_uid: int,
    expected_gid: int,
) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == mode
        and metadata.st_uid == expected_uid
        and metadata.st_gid == expected_gid
        and metadata.st_nlink == 1
        and metadata.st_size <= maximum
    )


def _read_exact_diagnostic_source(descriptor: int, expected_size: int) -> bytes:
    data = bytearray()
    while len(data) < expected_size:
        chunk = os.read(descriptor, min(64 * 1024, expected_size - len(data)))
        if not chunk:
            raise PermissionError("slot diagnostic redaction source is unsafe")
        data.extend(chunk)
    if os.read(descriptor, 1):
        raise PermissionError("slot diagnostic redaction source is unsafe")
    return bytes(data)


def _same_diagnostic_source(before: os.stat_result, after: os.stat_result) -> bool:
    attributes = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    return all(getattr(before, name) == getattr(after, name) for name in attributes)


def _secret_environment_values(data: bytes) -> tuple[str, ...]:
    text = data.decode("utf-8")
    values: dict[str, str] = {}
    for line in text.splitlines():
        name, value = _environment_assignment(line, values)
        if name in _SECRET_ENVIRONMENT_NAMES or _SECRET_ENVIRONMENT_PATTERN.search(name):
            values[name] = value
    _require_complete_secret_environment(values)
    return tuple(values[name] for name in sorted(values))


def _environment_assignment(line: str, values: dict[str, str]) -> tuple[str, str]:
    fields = shlex.split(line, posix=True)
    if len(fields) != 1 or "=" not in fields[0]:
        raise ValueError("worker environment is malformed")
    name, value = fields[0].split("=", 1)
    if not _ENVIRONMENT_NAME.fullmatch(name) or name in values:
        raise ValueError("worker environment is malformed")
    return name, value


def _require_complete_secret_environment(values: dict[str, str]) -> None:
    if _SECRET_ENVIRONMENT_NAMES.difference(values):
        raise ValueError("worker environment has incomplete secret sources")
    for value in values.values():
        if not value or len(value.encode("utf-8")) > 4096:
            raise ValueError("worker environment has incomplete secret sources")


class SystemdRuntime:
    """Observe and control only the eight fixed retained worker units."""

    def __init__(
        self,
        runner: CommandRunner,
        *,
        boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        proc_root: Path = Path("/proc"),
    ) -> None:
        self.runner = runner
        self.boot_id_path = boot_id_path
        self.cgroup_root = cgroup_root
        self.proc_root = proc_root

    def require_inactive(self, unit: str, deadline: Deadline) -> None:
        """Require the fixed unit to have no active state, cgroup, or invocation."""
        self._require_unit(unit)
        output = self.runner.run(
            ("systemctl", "show", _PROPERTY_ARGUMENT, unit),
            byte_limit=_CONTROL_OUTPUT_LIMIT,
            deadline=deadline,
        )
        properties = self._property_lines(output)
        self._require_inactive_properties(properties)

    def start(self, unit: str, deadline: Deadline) -> None:
        """Start one exact fixed unit through the injected bounded command runner."""
        self._require_unit(unit)
        self._run(("systemctl", "start", unit), deadline=deadline)

    def observe(self, unit: str, deadline: Deadline) -> SystemdObservation:
        """Return exact invocation evidence or an inactive unit's current boot evidence."""
        self._require_unit(unit)
        output = self.runner.run(
            ("systemctl", "show", _PROPERTY_ARGUMENT, unit),
            byte_limit=_CONTROL_OUTPUT_LIMIT,
            deadline=deadline,
        )
        boot_id = self._boot_id()
        properties = self._property_lines(output)
        if self._has_empty_inactive_identity(properties):
            return BootObservation(unit=unit, boot_id=boot_id)
        self._require_complete_properties(properties)
        control_group = properties["ControlGroup"]
        expected_group = f"{_WORKER_TEMPLATE_SLICE}/{unit}"
        if control_group != expected_group:
            raise SystemdConflict(f"systemd ControlGroup does not match fixed unit {unit}")
        invocation_id = properties["InvocationID"]
        if not _INVOCATION_ID.fullmatch(invocation_id):
            raise SystemdConflict("systemd InvocationID is not 32 lowercase hexadecimal bytes")
        status_value = properties["ExecMainStatus"]
        if not status_value.isascii() or not status_value.isdecimal():
            raise SystemdConflict("systemd ExecMainStatus is not an unsigned decimal status")
        exec_main_status = int(status_value, 10)
        if exec_main_status not in range(256):
            raise SystemdConflict("systemd ExecMainStatus is outside 0..255")
        return UnitObservation(
            unit=unit,
            boot_id=boot_id,
            invocation_id=invocation_id,
            active_state=properties["ActiveState"],
            sub_state=properties["SubState"],
            result=properties["Result"],
            exec_main_status=exec_main_status,
            control_group=control_group,
            membership=self._membership(control_group),
        )

    def signal_terminate(self, unit: str, deadline: Deadline) -> None:
        """Send SIGTERM to every process while preserving the retained unit."""
        self._require_unit(unit)
        self._run(
            (
                "systemctl",
                "kill",
                "--kill-whom=all",
                "--signal=SIGTERM",
                unit,
            ),
            deadline=deadline,
        )

    def stop_retained(self, unit: str, deadline: Deadline) -> None:
        """Stop one retained unit only after the caller has committed terminal evidence."""
        self._require_unit(unit)
        self._run(("systemctl", "stop", unit), deadline=deadline)

    def reset(self, unit: str, deadline: Deadline) -> None:
        """Reset one exact retained unit after the post-evidence stop path."""
        self._require_unit(unit)
        self._run(("systemctl", "reset-failed", unit), deadline=deadline)

    def unmanaged_workers(self) -> tuple[UnmanagedWorker, ...]:
        """List exact worker commands outside the eight fixed unit cgroups."""
        try:
            processes = tuple(self.proc_root.iterdir())
        except OSError as exc:
            raise SystemdUnavailable("cannot enumerate host processes") from exc
        unmanaged: list[UnmanagedWorker] = []
        for process in processes:
            if not process.name.isascii() or not process.name.isdecimal():
                continue
            worker = self._unmanaged_worker(process)
            if worker is not None:
                unmanaged.append(worker)
        return tuple(sorted(unmanaged))

    def journal(self, invocation_id: str, byte_limit: int, deadline: Deadline) -> str:
        """Read only one exact invocation's bounded journal stream."""
        if not _INVOCATION_ID.fullmatch(invocation_id):
            raise SystemdConflict("journal requires an exact systemd invocation identifier")
        if byte_limit not in range(1, _MAX_JOURNAL_BYTES + 1):
            raise ValueError(f"journal byte limit must be in 1..{_MAX_JOURNAL_BYTES}")
        return self.runner.run(
            ("journalctl", "--no-pager", f"_SYSTEMD_INVOCATION_ID={invocation_id}"),
            byte_limit=byte_limit,
            deadline=deadline,
            allow_truncation=True,
        )

    def public_properties(self, unit: str, invocation_id: str, deadline: Deadline) -> str:
        """Render only the public diagnostic allowlist for one retained invocation."""
        self._require_unit(unit)
        if not _INVOCATION_ID.fullmatch(invocation_id):
            raise SystemdConflict("diagnostic properties require an exact invocation identifier")
        output = self.runner.run(
            ("systemctl", "show", _PROPERTY_ARGUMENT, unit),
            byte_limit=_CONTROL_OUTPUT_LIMIT,
            deadline=deadline,
        )
        properties = self._property_lines(output)
        self._require_complete_properties(properties)
        if properties["InvocationID"] != invocation_id:
            raise SystemdConflict("diagnostic properties do not match the retained invocation")
        return "".join(f"{name}={properties[name]}\n" for name in _DIAGNOSTIC_PROPERTIES)

    def _unmanaged_worker(self, process: Path) -> UnmanagedWorker | None:
        try:
            is_worker = self._is_worker_command(process / "cmdline")
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise SystemdConflict(f"cannot inspect process {process.name} command") from exc
        if not is_worker:
            return None
        try:
            cgroup = self._process_cgroup(self._read_proc_file(process / "cgroup"))
            uid = self._process_uid(self._read_proc_file(process / "status"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise SystemdConflict(f"cannot verify process {process.name} cgroup and UID") from exc
        if _FIXED_WORKER_CGROUP.fullmatch(cgroup):
            return None
        return UnmanagedWorker(pid=int(process.name), uid=uid)

    @staticmethod
    def _read_proc_file(path: Path) -> bytes:
        with path.open("rb") as stream:
            data = stream.read(_PROC_FILE_LIMIT + 1)
        if len(data) > _PROC_FILE_LIMIT:
            raise ValueError("process metadata exceeds 4096 bytes")
        return data

    @classmethod
    def _is_worker_command(cls, path: Path) -> bool:
        with path.open("rb") as stream:
            launcher, ending, used = cls._read_command_token(stream, _PROC_FILE_LIMIT)
            if ending == "limit":
                raise ValueError("process launcher exceeds the exact command bound")
            if posixpath.basename(launcher) not in _PYTHON_LAUNCHERS:
                return False
            if ending != "nul":
                raise ValueError("worker launcher has no argument delimiter")
            remaining = _PROC_FILE_LIMIT - used
            for expected in (b"-m", b"kdive", b"worker"):
                matched, consumed = cls._read_expected_token(stream, expected, remaining)
                if not matched:
                    return False
                remaining -= consumed
            if stream.read(1):
                raise ValueError("worker command has unsupported trailing arguments")
            return True

    @staticmethod
    def _read_command_token(
        stream: BinaryIO, budget: int
    ) -> tuple[bytes, Literal["nul", "eof", "limit"], int]:
        token = bytearray()
        while len(token) < budget:
            value = stream.read(1)
            if not value:
                return bytes(token), "eof", len(token)
            if value == b"\0":
                return bytes(token), "nul", len(token) + 1
            token.extend(value)
        return bytes(token), "limit", budget

    @staticmethod
    def _read_expected_token(stream: BinaryIO, expected: bytes, budget: int) -> tuple[bool, int]:
        consumed = 0
        for offset in range(len(expected)):
            if consumed >= budget:
                raise ValueError("worker command is ambiguous at its byte limit")
            value = stream.read(1)
            if not value:
                raise ValueError("worker command ends inside a candidate argument")
            consumed += 1
            if value != expected[offset : offset + 1]:
                return False, consumed
        if consumed >= budget:
            raise ValueError("worker command is ambiguous at its byte limit")
        delimiter = stream.read(1)
        if not delimiter:
            raise ValueError("worker command has no exact argument delimiter")
        consumed += 1
        return delimiter == b"\0", consumed

    @staticmethod
    def _process_cgroup(data: bytes) -> bytes:
        lines = data.splitlines()
        if len(lines) != 1 or not lines[0].startswith(b"0::/"):
            raise ValueError("worker process has no exact cgroup-v2 membership")
        return lines[0][3:]

    @staticmethod
    def _process_uid(data: bytes) -> int:
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("worker process status is not ASCII") from exc
        uid_lines = [line for line in text.splitlines() if line.startswith("Uid:")]
        if len(uid_lines) != 1:
            raise ValueError("worker process status has no exact UID record")
        values = uid_lines[0].split()
        malformed = any(not value.isascii() or not value.isdecimal() for value in values[1:])
        if len(values) != 5 or malformed:
            raise ValueError("worker process UID record is malformed")
        return int(values[1])

    def _boot_id(self) -> str:
        try:
            with self.boot_id_path.open("rb") as stream:
                data = stream.read(130)
            boot_id = data.decode("ascii").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise SystemdUnavailable("host boot ID is unreadable") from exc
        if len(data) > 129 or not _BOOT_ID.fullmatch(boot_id):
            raise SystemdUnavailable("host boot ID is malformed")
        return boot_id

    def _membership(self, control_group: str) -> CgroupMembership:
        path = self.cgroup_root / control_group.removeprefix("/") / "cgroup.events"
        try:
            with path.open("rb") as stream:
                data = stream.read(_CGROUP_EVENTS_LIMIT + 1)
        except OSError:
            return "unknown"
        if len(data) > _CGROUP_EVENTS_LIMIT:
            return "unknown"
        return self._parse_membership(data)

    @staticmethod
    def _parse_membership(data: bytes) -> CgroupMembership:
        try:
            lines = data.decode("ascii").splitlines()
        except UnicodeDecodeError:
            return "unknown"
        parsed: dict[str, str] = {}
        for line in lines:
            match = re.fullmatch(r"(populated|frozen) ([01])", line)
            if match is None:
                return "unknown"
            key, value = match.groups()
            if key in parsed:
                return "unknown"
            parsed[key] = value
        populated = parsed.get("populated")
        if populated == "1":
            return "populated"
        if populated == "0":
            return "empty"
        return "unknown"

    @staticmethod
    def _property_lines(output: str) -> dict[str, str]:
        properties: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key not in _PROPERTIES:
                raise SystemdConflict("systemctl show returned a foreign property")
            if key in properties:
                raise SystemdConflict(f"systemctl show duplicated {key}")
            properties[key] = value
        return properties

    @staticmethod
    def _require_complete_properties(properties: dict[str, str]) -> None:
        for key, value in properties.items():
            if not value:
                raise SystemdUnavailable(f"systemctl show did not return a non-empty {key}")
        for key in _PROPERTIES:
            if key not in properties:
                raise SystemdUnavailable(f"systemctl show did not return {key}")
        for key in ("ActiveState", "SubState", "Result"):
            if not _SYSTEMD_VALUE.fullmatch(properties[key]):
                raise SystemdConflict(f"systemctl show returned a malformed {key}")

    @staticmethod
    def _has_empty_inactive_identity(properties: dict[str, str]) -> bool:
        missing = tuple(key for key in _PROPERTIES if key not in properties)
        if missing:
            for key, value in properties.items():
                if not value:
                    raise SystemdUnavailable(f"systemctl show did not return a non-empty {key}")
            raise SystemdUnavailable(f"systemctl show did not return {missing[0]}")
        control_group = properties["ControlGroup"]
        invocation_id = properties["InvocationID"]
        if bool(control_group) != bool(invocation_id):
            raise SystemdConflict("systemctl show returned a partial unit identity")
        if control_group:
            return False
        SystemdRuntime._require_inactive_properties(properties)
        return True

    @staticmethod
    def _require_inactive_properties(properties: dict[str, str]) -> None:
        for key in _PROPERTIES:
            if key not in properties:
                raise SystemdUnavailable(f"systemctl show did not return {key}")
        for key in ("ActiveState", "SubState", "Result"):
            if not _SYSTEMD_VALUE.fullmatch(properties[key]):
                raise SystemdConflict(f"systemctl show returned a malformed {key}")
        status = properties["ExecMainStatus"]
        if not status.isascii() or not status.isdecimal() or int(status, 10) not in range(256):
            raise SystemdConflict("systemctl show returned a malformed ExecMainStatus")
        if (
            properties["ActiveState"] != "inactive"
            or properties["SubState"] != "dead"
            or properties["ControlGroup"]
            or properties["InvocationID"]
        ):
            raise SystemdConflict("fixed worker unit is not inactive with empty identity")

    @staticmethod
    def _require_unit(unit: str) -> None:
        if not _UNIT.fullmatch(unit):
            raise SystemdConflict("operation requires one fixed worker unit")

    def _run(self, argv: tuple[str, ...], *, deadline: Deadline | None = None) -> None:
        self.runner.run(argv, byte_limit=_CONTROL_OUTPUT_LIMIT, deadline=deadline)
