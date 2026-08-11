"""Exact, bounded systemd evidence and control for fixed host worker slots."""

from __future__ import annotations

import math
import os
import re
import selectors
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

type CgroupMembership = Literal["populated", "empty", "unknown"]

_UNIT = re.compile(r"kdive-live-worker@[1-8]\.service")
_PYTHON_LAUNCHER = re.compile(rb"(?:/(?:[^/\0]+/)*)?python(?:3(?:\.14)?)?")
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
_CONTROL_OUTPUT_LIMIT = 4096
_CGROUP_EVENTS_LIMIT = 4096
_PROC_FILE_LIMIT = 4096
_MAX_JOURNAL_BYTES = 320 * 1024


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
        if operation_deadline.remaining() <= 0:
            raise CommandDeadlineExceeded("command deadline elapsed before child launch")
        process = self._launch(argv)
        output, truncated = self._collect_with_cleanup(process, byte_limit, operation_deadline)
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
        self, process: subprocess.Popen[bytes], byte_limit: int, deadline: Deadline
    ) -> tuple[bytes, bool]:
        try:
            return self._collect(process, byte_limit, deadline)
        except BaseException as exc:
            try:
                self._terminate(process, deadline)
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
        if cls._wait_for_cleanup(process, deadline, ceiling=0.25):
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


@dataclass(frozen=True, slots=True, order=True)
class UnmanagedWorker:
    """A live ``kdive worker`` process outside every fixed worker unit cgroup."""

    pid: int
    uid: int


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

    def start(self, unit: str) -> None:
        """Start one exact fixed unit through the injected bounded command runner."""
        self._require_unit(unit)
        self._run(("systemctl", "start", unit))

    def observe(self, unit: str) -> UnitObservation:
        """Return only complete, exact manager and recursive-cgroup evidence."""
        self._require_unit(unit)
        output = self.runner.run(
            ("systemctl", "show", _PROPERTY_ARGUMENT, unit),
            byte_limit=_CONTROL_OUTPUT_LIMIT,
        )
        properties = self._parse_properties(output)
        control_group = properties["ControlGroup"]
        expected_group = f"/system.slice/{unit}"
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
            boot_id=self._boot_id(),
            invocation_id=invocation_id,
            active_state=properties["ActiveState"],
            sub_state=properties["SubState"],
            result=properties["Result"],
            exec_main_status=exec_main_status,
            control_group=control_group,
            membership=self._membership(control_group),
        )

    def signal_terminate(self, unit: str) -> None:
        """Send SIGTERM to every process while preserving the retained unit."""
        self._require_unit(unit)
        self._run(
            (
                "systemctl",
                "kill",
                "--kill-whom=all",
                "--signal=SIGTERM",
                unit,
            )
        )

    def stop_retained(self, unit: str, deadline: Deadline) -> None:
        """Stop one retained unit only after the caller has committed terminal evidence."""
        self._require_unit(unit)
        self._run(("systemctl", "stop", unit), deadline=deadline)

    def reset(self, unit: str) -> None:
        """Reset one exact retained unit after the post-evidence stop path."""
        self._require_unit(unit)
        self._run(("systemctl", "reset-failed", unit))

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
        if re.fullmatch(rb"/system\.slice/kdive-live-worker@[1-8]\.service", cgroup):
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
                if launcher.startswith(b"/"):
                    raise ValueError("process launcher exceeds the exact command bound")
                return False
            if not _PYTHON_LAUNCHER.fullmatch(launcher):
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
    def _parse_properties(output: str) -> dict[str, str]:
        properties = SystemdRuntime._property_lines(output)
        SystemdRuntime._require_complete_properties(properties)
        return properties

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
    def _require_unit(unit: str) -> None:
        if not _UNIT.fullmatch(unit):
            raise SystemdConflict("operation requires one fixed worker unit")

    def _run(self, argv: tuple[str, ...], *, deadline: Deadline | None = None) -> None:
        self.runner.run(argv, byte_limit=_CONTROL_OUTPUT_LIMIT, deadline=deadline)
