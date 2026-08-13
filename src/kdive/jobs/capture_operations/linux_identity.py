"""PID-reuse-safe Linux child identity and pidfd operations (ADR-0558)."""

from __future__ import annotations

import errno
import os
import signal
from dataclasses import dataclass
from pathlib import Path

_PROC_ROOT = Path("/proc")


def _read_cmdline(path: Path) -> bytes:
    return path.read_bytes()


def _matches_capture_bootstrap(argv: list[bytes], token: str) -> bool:
    try:
        module = argv.index(b"kdive.capture_bootstrap")
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
    """One exact process in one Linux boot, identified by `/proc` start ticks."""

    boot_id: str
    pid: int
    start_ticks: int

    @classmethod
    def read(cls, pid: int) -> LinuxIdentity:
        """Read an exact process identity from the boot id and `/proc/<pid>/stat`."""
        if pid <= 0:
            raise ValueError("pid must be positive")
        try:
            boot_id = (_PROC_ROOT / "sys/kernel/random/boot_id").read_text().strip()
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
        return cls(boot_id=boot_id, pid=pid, start_ticks=start_ticks)

    def open_pidfd(self) -> int:
        """Open a pidfd and recheck boot/start ticks to close the observation race."""
        try:
            pidfd = os.pidfd_open(self.pid, 0)
        except OSError as error:
            if error.errno == errno.ESRCH:
                raise ProcessLookupError(errno.ESRCH, f"process {self.pid} is absent") from error
            raise
        try:
            if LinuxIdentity.read(self.pid) != self:
                raise ProcessLookupError(errno.ESRCH, f"process {self.pid} identity changed")
        except BaseException:
            os.close(pidfd)
            raise
        return pidfd

    def signal(self, pidfd: int, sig: int) -> None:
        """Signal this exact process through its already-open pidfd."""
        signal.pidfd_send_signal(pidfd, int(sig), None, 0)

    def is_absent(self) -> bool:
        """Return true when the boot changed, the PID vanished, or the PID was reused."""
        try:
            return LinuxIdentity.read(self.pid) != self
        except ProcessLookupError:
            return True


def scan_launch_token(
    launch_token: str, *, interpreter: Path, expected_uid: int | None = None
) -> tuple[LinuxIdentity, ...]:
    """Completely enumerate `/proc` for pre-registration children carrying one exact token.

    Any unreadable surviving process makes the scan inconclusive. Callers terminate returned
    identities through pidfds and run this complete scan twice before acknowledging token absence.
    """
    if len(launch_token) != 64 or any(
        character not in "0123456789abcdef" for character in launch_token
    ):
        raise ValueError("launch token must be 64 lowercase hexadecimal characters")
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
            identity = LinuxIdentity.read(int(entry.name))
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(f"complete launch-token scan could not attest {entry}") from error
        if observed_executable == executable:
            matches.append(identity)
    return tuple(sorted(matches, key=lambda identity: identity.pid))
