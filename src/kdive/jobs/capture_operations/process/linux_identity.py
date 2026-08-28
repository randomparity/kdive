"""PID-reuse-safe Linux child identity and pidfd operations (ADR-0558)."""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path

from kdive.jobs.capture_operations.process import linux_pidfd

_PROC_ROOT = Path("/proc")


class HostIdentityMismatch(RuntimeError):
    """The observer is not on the durable worker-host boundary."""


def _validate_host_instance(host_instance: str) -> str:
    if not 1 <= len(host_instance.encode()) <= 512:
        raise ValueError("host instance must contain 1..512 bytes")
    return host_instance


def _read_cmdline(path: Path) -> bytes:
    return path.read_bytes()


def _matches_capture_bootstrap(argv: list[bytes], token: str) -> bool:
    try:
        module = argv.index(b"kdive.jobs.capture_operations.bootstrap.bootstrap_entrypoint")
        token_flag = argv.index(b"--launch-token")
    except ValueError:
        return False
    return (
        module >= 2
        and argv[module - 1] == b"-m"
        and token_flag == module + 1
        and token_flag + 1 < len(argv)
        and argv[token_flag + 1] == token.encode()
    )


@dataclass(frozen=True, slots=True)
class LinuxIdentity:
    """One exact process on one authority-bound host and Linux boot."""

    host_instance: str
    boot_id: str
    pid: int
    start_ticks: int

    @classmethod
    def read(cls, pid: int, *, host_instance: str) -> LinuxIdentity:
        """Read an exact process identity on the supplied Task 1 host boundary."""
        if pid <= 0:
            raise ValueError("pid must be positive")
        stable_host = _validate_host_instance(host_instance)
        try:
            boot_id = (_PROC_ROOT / "sys/kernel/random/boot_id").read_text().strip()
        except FileNotFoundError as error:
            raise RuntimeError("boot id sentinel is unreadable") from error
        try:
            stat_line = (_PROC_ROOT / str(pid) / "stat").read_text().strip()
        except FileNotFoundError as error:
            raise ProcessLookupError(errno.ESRCH, f"process {pid} is absent") from error
        closing = stat_line.rfind(")")
        fields = stat_line[closing + 2 :].split() if closing > 1 else []
        if not boot_id or len(fields) <= 19:
            raise RuntimeError(f"malformed /proc/{pid}/stat or boot id")
        try:
            start_ticks = int(fields[19])
        except ValueError as error:
            raise RuntimeError(f"malformed /proc/{pid}/stat start ticks") from error
        if start_ticks < 0:
            raise RuntimeError(f"malformed /proc/{pid}/stat start ticks")
        return cls(
            host_instance=stable_host,
            boot_id=boot_id,
            pid=pid,
            start_ticks=start_ticks,
        )

    def _require_host(self, current_host_instance: str) -> None:
        current = _validate_host_instance(current_host_instance)
        if current != self.host_instance:
            raise HostIdentityMismatch(
                f"durable host {self.host_instance} does not match observer host {current}"
            )

    def open_pidfd(self, *, current_host_instance: str) -> int:
        """Open a pidfd and recheck boot/start ticks to close the observation race."""
        self._require_host(current_host_instance)
        try:
            pidfd = linux_pidfd.open_pidfd(self.pid, 0)
        except OSError as error:
            if error.errno == errno.ESRCH:
                raise ProcessLookupError(errno.ESRCH, f"process {self.pid} is absent") from error
            raise
        try:
            if LinuxIdentity.read(self.pid, host_instance=current_host_instance) != self:
                raise ProcessLookupError(errno.ESRCH, f"process {self.pid} identity changed")
        except BaseException:
            os.close(pidfd)
            raise
        return pidfd

    def signal(self, pidfd: int, sig: int) -> None:
        """Signal this exact process through its already-open pidfd."""
        linux_pidfd.send_signal(pidfd, int(sig), None, 0)

    def is_absent(self, *, current_host_instance: str) -> bool:
        """Return true for same-host absence; refuse to infer across host boundaries."""
        self._require_host(current_host_instance)
        try:
            return LinuxIdentity.read(self.pid, host_instance=current_host_instance) != self
        except ProcessLookupError:
            return True


def scan_launch_token(
    launch_token: str,
    *,
    interpreter: Path,
    host_instance: str,
    expected_uid: int | None = None,
) -> tuple[LinuxIdentity, ...]:
    """Completely enumerate `/proc` for pre-registration children carrying one exact token.

    Any unreadable surviving process makes the scan inconclusive. Callers terminate returned
    identities through pidfds and run this complete scan twice before acknowledging token absence.
    """
    if len(launch_token) != 64 or any(
        character not in "0123456789abcdef" for character in launch_token
    ):
        raise ValueError("launch token must be 64 lowercase hexadecimal characters")
    stable_host = _validate_host_instance(host_instance)
    owner = os.geteuid() if expected_uid is None else expected_uid
    executable = interpreter.resolve(strict=True)
    try:
        entries = list(_PROC_ROOT.iterdir())
    except OSError as error:
        raise RuntimeError("complete launch-token scan could not enumerate /proc") from error
    matches: list[LinuxIdentity] = []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != owner:
                continue
            raw_cmdline = _read_cmdline(entry / "cmdline")
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(f"complete launch-token scan could not read {entry}") from error
        argv = [part for part in raw_cmdline.split(b"\0") if part]
        if not _matches_capture_bootstrap(argv, launch_token):
            continue
        try:
            observed_executable = (entry / "exe").resolve(strict=True)
            identity = LinuxIdentity.read(int(entry.name), host_instance=stable_host)
        except FileNotFoundError, ProcessLookupError:
            continue
        except OSError as error:
            raise RuntimeError(f"complete launch-token scan could not attest {entry}") from error
        if observed_executable == executable:
            matches.append(identity)
    return tuple(sorted(matches, key=lambda identity: identity.pid))
