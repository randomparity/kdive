"""Exact Linux process identity tests (ADR-0558)."""

from __future__ import annotations

import errno
import os
import signal
from pathlib import Path

import pytest

from kdive.jobs.capture_operations import linux_identity, linux_pidfd
from kdive.jobs.capture_operations.linux_identity import (
    HostIdentityMismatch,
    LinuxIdentity,
    scan_launch_token,
)


def _stat_text(pid: int, start_ticks: int) -> str:
    # comm may contain spaces and ')'; starttime is field 22 after the final ')'.
    tail = ["S", *(["0"] * 18), str(start_ticks)]
    return f"{pid} (odd ) name) {' '.join(tail)}\n"


def _proc_tree(root: Path, pid: int, *, start_ticks: int = 4242) -> None:
    (root / "sys/kernel/random").mkdir(parents=True)
    (root / "sys/kernel/random/boot_id").write_text("boot-a\n")
    proc = root / str(pid)
    proc.mkdir()
    (proc / "stat").write_text(_stat_text(pid, start_ticks))


def _register_bootstrap(proc: Path, *, interpreter: Path, token: str) -> None:
    """Make ``proc`` (an already-created /proc/<pid> dir) match scan_launch_token's
    capture-bootstrap probe: same exe target, cmdline carrying the exact token."""
    proc.joinpath("exe").symlink_to(interpreter)
    proc.joinpath("cmdline").write_bytes(
        b"python\0-S\0-m\0kdive.capture_bootstrap\0--launch-token\0" + token.encode() + b"\0"
    )


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
    monkeypatch.setattr(linux_pidfd, "open_pidfd", lambda pid, flags=0: 17)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        linux_pidfd,
        "send_signal",
        lambda fd, sig, _info=None, _flags=0: sent.append((fd, sig)),
    )
    identity = LinuxIdentity.read(123, host_instance="host-a")

    assert identity.open_pidfd(current_host_instance="host-a") == 17
    identity.signal(17, signal.SIGTERM)
    assert sent == [(17, signal.SIGTERM)]


def test_pidfd_open_surfaces_disappearance_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = LinuxIdentity(host_instance="host-a", boot_id="boot-a", pid=123, start_ticks=42)
    monkeypatch.setattr(
        linux_pidfd,
        "open_pidfd",
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

    monkeypatch.setattr(linux_pidfd, "open_pidfd", _pidfd_open)

    with pytest.raises(HostIdentityMismatch, match="host-a.*host-b"):
        identity.open_pidfd(current_host_instance="host-b")
    assert not opened


def test_fallback_pidfd_is_closed_when_identity_recheck_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc_tree(tmp_path, 123, start_ticks=99)
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)
    pidfd = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)

    class _Open:
        argtypes: list[object] = []
        restype: object = None

        def __call__(self, _pid: int, _flags: int) -> int:
            return pidfd

    class _Libc:
        pidfd_open = _Open()

    monkeypatch.delattr(linux_pidfd.os, "pidfd_open", raising=False)
    monkeypatch.setattr(linux_pidfd, "_LIBC", _Libc())
    identity = LinuxIdentity(host_instance="host-a", boot_id="boot-a", pid=123, start_ticks=98)

    with pytest.raises(ProcessLookupError, match="identity changed"):
        identity.open_pidfd(current_host_instance="host-a")
    with pytest.raises(OSError) as raised:
        os.fstat(pidfd)
    assert raised.value.errno == errno.EBADF


def test_launch_token_scan_finds_only_exact_executable_and_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc_tree(tmp_path, 123)
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"python")
    token = "a" * 64
    _register_bootstrap(tmp_path / "123", interpreter=interpreter, token=token)
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)

    assert scan_launch_token(token, interpreter=interpreter, host_instance="host-a") == (
        LinuxIdentity(host_instance="host-a", boot_id="boot-a", pid=123, start_ticks=4242),
    )


def test_launch_token_scan_skips_matched_process_that_exits_before_identity_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matched process vanishing between the cmdline read and the identity read is skipped,
    not turned into a scan-wide RuntimeError (regression test for #1974).

    ``LinuxIdentity.read`` reports a vanished process as ``ProcessLookupError``, not
    ``FileNotFoundError`` — drive that exact exception through the real second-stage
    call site rather than patching the ``except`` arm directly.
    """
    (tmp_path / "sys/kernel/random").mkdir(parents=True)
    (tmp_path / "sys/kernel/random/boot_id").write_text("boot-a\n")
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"python")
    token = "a" * 64
    for pid, start_ticks in ((111, 1111), (222, 2222)):
        proc = tmp_path / str(pid)
        proc.mkdir()
        proc.joinpath("stat").write_text(_stat_text(pid, start_ticks))
        _register_bootstrap(proc, interpreter=interpreter, token=token)
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)
    real_read = LinuxIdentity.read

    def _flaky_read(pid: int, *, host_instance: str) -> LinuxIdentity:
        if pid == 111:
            raise ProcessLookupError(errno.ESRCH, f"process {pid} is absent")
        return real_read(pid, host_instance=host_instance)

    monkeypatch.setattr(LinuxIdentity, "read", _flaky_read)

    assert scan_launch_token(token, interpreter=interpreter, host_instance="host-a") == (
        LinuxIdentity(host_instance="host-a", boot_id="boot-a", pid=222, start_ticks=2222),
    )


def test_launch_token_scan_raises_for_genuine_identity_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ESRCH OSError from the identity read is a genuine failure, not a vanished
    process, and must still abort the scan rather than being widened into a skip
    (acceptance criteria for #1974)."""
    _proc_tree(tmp_path, 123)
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"python")
    token = "a" * 64
    _register_bootstrap(tmp_path / "123", interpreter=interpreter, token=token)
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(
        LinuxIdentity,
        "read",
        lambda _pid, *, host_instance: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with pytest.raises(RuntimeError, match="complete launch-token scan could not attest"):
        scan_launch_token(token, interpreter=interpreter, host_instance="host-a")


def test_launch_token_scan_raises_when_boot_id_sentinel_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LinuxIdentity.read`` reports a vanished pid and an unreadable shared boot_id sentinel
    as the same ``ProcessLookupError`` shape. Only the pid-specific case means "this matched
    process vanished, skip it" — an unreadable boot_id is a genuine scan failure and must
    still raise RuntimeError, not be silently absorbed by the vanished-process skip."""
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"python")
    token = "a" * 64
    proc = tmp_path / "123"
    proc.mkdir()
    proc.joinpath("stat").write_text(_stat_text(123, 4242))
    _register_bootstrap(proc, interpreter=interpreter, token=token)
    # deliberately no sys/kernel/random/boot_id under tmp_path
    monkeypatch.setattr(linux_identity, "_PROC_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="boot id sentinel"):
        scan_launch_token(token, interpreter=interpreter, host_instance="host-a")


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
