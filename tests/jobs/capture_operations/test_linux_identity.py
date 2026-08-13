"""Exact Linux process identity tests (ADR-0558)."""

from __future__ import annotations

import errno
import signal
from pathlib import Path

import pytest

from kdive.jobs.capture_operations import linux_identity
from kdive.jobs.capture_operations.linux_identity import (
    HostIdentityMismatch,
    LinuxIdentity,
    scan_launch_token,
)


def _proc_tree(root: Path, pid: int, *, start_ticks: int = 4242) -> None:
    (root / "sys/kernel/random").mkdir(parents=True)
    (root / "sys/kernel/random/boot_id").write_text("boot-a\n")
    proc = root / str(pid)
    proc.mkdir()
    # comm may contain spaces and ')'; starttime is field 22 after the final ')'.
    tail = ["S", *(["0"] * 18), str(start_ticks)]
    (proc / "stat").write_text(f"{pid} (odd ) name) {' '.join(tail)}\n")


def test_linux_identity_reads_boot_pid_and_exact_start_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc_tree(tmp_path, 123)
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)

    assert LinuxIdentity.read(123, host_instance="host-a") == LinuxIdentity(
        host_instance="host-a", boot_id="boot-a", pid=123, start_ticks=4242
    )


def test_linux_identity_absence_includes_pid_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc_tree(tmp_path, 123, start_ticks=99)
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)
    identity = LinuxIdentity(host_instance="host-a", boot_id="boot-a", pid=123, start_ticks=98)

    assert identity.is_absent(current_host_instance="host-a")


def test_linux_identity_host_mismatch_is_inconclusive_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc_tree(tmp_path, 123)
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)
    identity = LinuxIdentity.read(123, host_instance="host-a")

    with pytest.raises(HostIdentityMismatch, match="host-a.*host-b"):
        identity.is_absent(current_host_instance="host-b")


def test_linux_identity_refuses_unreadable_or_malformed_proc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc_tree(tmp_path, 123)
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)
    (tmp_path / "123/stat").write_text("malformed")

    with pytest.raises(RuntimeError, match="/proc/123/stat"):
        LinuxIdentity.read(123, host_instance="host-a")


def test_pidfd_open_rechecks_identity_and_signal_uses_pidfd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc_tree(tmp_path, 123)
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(linux_identity.os, "pidfd_open", lambda pid, flags=0: 17)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        linux_identity.signal,
        "pidfd_send_signal",
        lambda fd, sig, _info=None, _flags=0: sent.append((fd, sig)),
    )
    identity = LinuxIdentity.read(123, host_instance="host-a")

    assert identity.open_pidfd(current_host_instance="host-a") == 17
    identity.signal(17, signal.SIGTERM)
    assert sent == [(17, signal.SIGTERM)]


def test_pidfd_open_surfaces_disappearance_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = LinuxIdentity(host_instance="host-a", boot_id="boot-a", pid=123, start_ticks=42)
    monkeypatch.setattr(
        linux_identity.os,
        "pidfd_open",
        lambda _pid, _flags=0: (_ for _ in ()).throw(OSError(errno.ESRCH, "gone")),
    )

    with pytest.raises(ProcessLookupError):
        identity.open_pidfd(current_host_instance="host-a")


def test_pidfd_open_refuses_cross_host_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = LinuxIdentity(host_instance="host-a", boot_id="boot-a", pid=123, start_ticks=42)
    opened = False

    def _pidfd_open(_pid: int, _flags: int = 0) -> int:
        nonlocal opened
        opened = True
        return 17

    monkeypatch.setattr(linux_identity.os, "pidfd_open", _pidfd_open)

    with pytest.raises(HostIdentityMismatch, match="host-a.*host-b"):
        identity.open_pidfd(current_host_instance="host-b")
    assert not opened


def test_launch_token_scan_finds_only_exact_executable_and_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc_tree(tmp_path, 123)
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"python")
    (tmp_path / "123/exe").symlink_to(interpreter)
    token = "a" * 64
    (tmp_path / "123/cmdline").write_bytes(
        b"python\0-S\0-m\0kdive.capture_bootstrap\0--launch-token\0" + token.encode() + b"\0"
    )
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)

    assert scan_launch_token(token, interpreter=interpreter, host_instance="host-a") == (
        LinuxIdentity(host_instance="host-a", boot_id="boot-a", pid=123, start_ticks=4242),
    )


def test_launch_token_scan_refuses_incomplete_proc_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc_tree(tmp_path, 123)
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"python")
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(
        linux_identity,
        "_read_cmdline",
        lambda _path: (_ for _ in ()).throw(PermissionError("hidden proc")),
    )

    with pytest.raises(RuntimeError, match="complete launch-token scan"):
        scan_launch_token("a" * 64, interpreter=interpreter, host_instance="host-a")
