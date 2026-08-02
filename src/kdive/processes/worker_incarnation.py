"""Non-reusable local worker identity and authoritative death verification."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_PROC_ROOT = Path("/proc")


def _start_ticks(stat: str) -> str:
    """Extract Linux ``/proc/PID/stat`` field 22 despite spaces in ``comm``."""
    close = stat.rfind(")")
    fields = stat[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19 or not fields[19].isdigit():
        raise RuntimeError("worker process stat has no valid start-time field")
    return fields[19]


def worker_incarnation_id(
    pid: int,
    *,
    boot_id_path: Path = _BOOT_ID,
    stat_path: Path | None = None,
) -> str:
    """Return ``host:pid:boot-id:start-ticks`` for this exact Linux process."""
    boot_id = boot_id_path.read_text(encoding="utf-8").strip()
    stat = (stat_path or (_PROC_ROOT / str(pid) / "stat")).read_text(encoding="utf-8")
    return f"{socket.gethostname()}:{pid}:{boot_id}:{_start_ticks(stat)}"


@dataclass(frozen=True, slots=True)
class LocalWorkerDeathVerifier:
    """Prove a local incarnation absent from Linux process identity, never from heartbeat age."""

    boot_id_path: Path = _BOOT_ID
    proc_root: Path = _PROC_ROOT

    def verify_dead(self, worker_incarnation: str) -> str | None:
        """Return bounded authoritative evidence, or ``None`` when death is not proven."""
        try:
            host, raw_pid, expected_boot, expected_start = worker_incarnation.rsplit(":", 3)
            pid = int(raw_pid)
        except TypeError, ValueError:
            return None
        if host != socket.gethostname() or pid <= 0:
            return None
        current_boot = self.boot_id_path.read_text(encoding="utf-8").strip()
        if current_boot != expected_boot:
            return "local-proc: exact worker incarnation absent (host rebooted)"
        try:
            current_start = _start_ticks(
                (self.proc_root / str(pid) / "stat").read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return "local-proc: exact worker incarnation absent (pid absent)"
        except OSError, RuntimeError:
            return None
        if current_start != expected_start:
            return "local-proc: exact worker incarnation absent (pid start changed)"
        return None
