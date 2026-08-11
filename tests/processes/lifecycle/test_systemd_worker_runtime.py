"""Exact and bounded systemd worker runtime evidence."""

from __future__ import annotations

import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

import pytest

from kdive.processes.lifecycle.systemd_worker_runtime import (
    BootObservation,
    CommandCleanupDeadlineExceeded,
    CommandDeadlineExceeded,
    CommandOutputTooLarge,
    MonotonicDeadline,
    SubprocessCommandRunner,
    SystemdConflict,
    SystemdRuntime,
    SystemdUnavailable,
    UnitObservation,
    UnmanagedWorker,
)

_BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"
_INVOCATION_ID = "a" * 32
_PROPERTIES = (
    "ActiveState=active\n"
    "SubState=running\n"
    "Result=success\n"
    "ExecMainStatus=0\n"
    "ControlGroup=/system.slice/kdive-live-worker@1.service\n"
    f"InvocationID={_INVOCATION_ID}\n"
)
_INACTIVE_PROPERTIES = (
    "ActiveState=inactive\n"
    "SubState=dead\n"
    "Result=success\n"
    "ExecMainStatus=0\n"
    "ControlGroup=\n"
    "InvocationID=\n"
)


class FakeDeadline:
    """Fixed remaining-time view for command-port assertions."""

    def __init__(self, remaining: float) -> None:
        self.value = remaining

    def remaining(self) -> float:
        return self.value


class ScriptedDeadline:
    """Return exact remaining-time readings for cleanup assertions."""

    def __init__(self, remaining: Sequence[float]) -> None:
        self.values = iter(remaining)

    def remaining(self) -> float:
        return next(self.values)


class UnreapableProcess:
    """Record cleanup calls while refusing each bounded wait."""

    def __init__(self) -> None:
        self.calls: list[str | tuple[str, float | None]] = []

    def poll(self) -> None:
        self.calls.append("poll")

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(("wait", timeout))
        if timeout is None:
            pytest.fail("cleanup wait must carry the remaining deadline budget")
        raise subprocess.TimeoutExpired("fake-child", timeout)


class FakeRunner:
    """Injected command boundary returning one complete systemctl response."""

    def __init__(self, output: str = _PROPERTIES) -> None:
        self.output = output
        self.calls: list[tuple[str, ...]] = []
        self.deadlines: list[object | None] = []
        self.byte_limits: list[int] = []
        self.truncations: list[bool] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        byte_limit: int,
        deadline: object | None = None,
        allow_truncation: bool = False,
    ) -> str:
        self.calls.append(tuple(argv))
        self.deadlines.append(deadline)
        self.byte_limits.append(byte_limit)
        self.truncations.append(allow_truncation)
        return self.output


@pytest.fixture
def fake_host(tmp_path: Path) -> tuple[Path, Path]:
    boot_id_path = tmp_path / "proc/sys/kernel/random/boot_id"
    boot_id_path.parent.mkdir(parents=True)
    boot_id_path.write_text(f"{_BOOT_ID}\n", encoding="ascii")
    cgroup_root = tmp_path / "sys/fs/cgroup"
    events = cgroup_root / "system.slice/kdive-live-worker@1.service/cgroup.events"
    events.parent.mkdir(parents=True)
    events.write_text("populated 1\nfrozen 0\n", encoding="ascii")
    return boot_id_path, cgroup_root


def _runtime(fake_host: tuple[Path, Path], runner: FakeRunner) -> SystemdRuntime:
    boot_id_path, cgroup_root = fake_host
    return SystemdRuntime(runner, boot_id_path=boot_id_path, cgroup_root=cgroup_root)


def test_observe_uses_one_fixed_property_query_and_exact_cgroup(
    fake_host: tuple[Path, Path],
) -> None:
    runner = FakeRunner()

    observation = _runtime(fake_host, runner).observe(
        "kdive-live-worker@1.service", FakeDeadline(120.0)
    )

    assert isinstance(observation, UnitObservation)
    assert runner.calls == [
        (
            "systemctl",
            "show",
            "--property=ActiveState,SubState,Result,ExecMainStatus,ControlGroup,InvocationID",
            "kdive-live-worker@1.service",
        )
    ]
    assert observation.unit == "kdive-live-worker@1.service"
    assert observation.boot_id == _BOOT_ID
    assert observation.invocation_id == _INVOCATION_ID
    assert observation.active_state == "active"
    assert observation.sub_state == "running"
    assert observation.result == "success"
    assert observation.exec_main_status == 0
    assert observation.control_group == "/system.slice/kdive-live-worker@1.service"
    assert observation.membership == "populated"


def test_observe_returns_boot_only_evidence_for_inactive_empty_unit(
    fake_host: tuple[Path, Path],
) -> None:
    observation = _runtime(fake_host, FakeRunner(_INACTIVE_PROPERTIES)).observe(
        "kdive-live-worker@1.service", FakeDeadline(120.0)
    )

    assert observation == BootObservation(
        unit="kdive-live-worker@1.service",
        boot_id=_BOOT_ID,
    )


@pytest.mark.parametrize(
    "output",
    [
        _INACTIVE_PROPERTIES.replace(
            "ControlGroup=\n", "ControlGroup=/system.slice/kdive-live-worker@1.service\n"
        ),
        _INACTIVE_PROPERTIES.replace("InvocationID=\n", f"InvocationID={_INVOCATION_ID}\n"),
    ],
)
def test_observe_rejects_partial_inactive_identity(
    fake_host: tuple[Path, Path], output: str
) -> None:
    with pytest.raises(SystemdConflict, match="identity"):
        _runtime(fake_host, FakeRunner(output)).observe(
            "kdive-live-worker@1.service", FakeDeadline(120.0)
        )


def test_observe_requires_every_exact_property(fake_host: tuple[Path, Path]) -> None:
    runner = FakeRunner("ActiveState=active\nInvocationID=\nControlGroup=/x\n")
    with pytest.raises(SystemdUnavailable, match="InvocationID"):
        _runtime(fake_host, runner).observe("kdive-live-worker@1.service", FakeDeadline(120.0))


@pytest.mark.parametrize(
    "output",
    [
        _PROPERTIES + "InvocationID=" + "b" * 32 + "\n",
        _PROPERTIES + "Description=foreign\n",
        _PROPERTIES.replace("ExecMainStatus=0", "ExecMainStatus=not-an-integer"),
        _PROPERTIES.replace("ExecMainStatus=0", "ExecMainStatus=+1"),
        _PROPERTIES.replace(
            "/system.slice/kdive-live-worker@1.service",
            "/system.slice/kdive-live-worker@2.service",
        ),
    ],
)
def test_observe_rejects_ambiguous_or_conflicting_properties(
    fake_host: tuple[Path, Path], output: str
) -> None:
    with pytest.raises(SystemdConflict):
        _runtime(fake_host, FakeRunner(output)).observe(
            "kdive-live-worker@1.service", FakeDeadline(120.0)
        )


@pytest.mark.parametrize(
    ("events", "membership"),
    [
        ("populated 1\nfrozen 0\n", "populated"),
        ("populated 0\nfrozen 0\n", "empty"),
        ("populated maybe\n", "unknown"),
        ("populated 0\npopulated 0\n", "unknown"),
        ("populated 0\nforeign 0\n", "unknown"),
        ("populated    0\n", "unknown"),
        ("frozen 0\n", "unknown"),
    ],
)
def test_observe_reads_recursive_cgroup_membership(
    fake_host: tuple[Path, Path],
    events: str,
    membership: Literal["populated", "empty", "unknown"],
) -> None:
    _, cgroup_root = fake_host
    path = cgroup_root / "system.slice/kdive-live-worker@1.service/cgroup.events"
    path.write_text(events, encoding="ascii")

    observation = _runtime(fake_host, FakeRunner()).observe(
        "kdive-live-worker@1.service", FakeDeadline(120.0)
    )
    assert cast(UnitObservation, observation).membership == membership


def test_missing_unreadable_and_oversized_membership_are_unknown(
    fake_host: tuple[Path, Path],
) -> None:
    _, cgroup_root = fake_host
    path = cgroup_root / "system.slice/kdive-live-worker@1.service/cgroup.events"
    path.unlink()
    runtime = _runtime(fake_host, FakeRunner())
    observation = runtime.observe("kdive-live-worker@1.service", FakeDeadline(120.0))
    assert cast(UnitObservation, observation).membership == "unknown"

    path.mkdir()
    observation = runtime.observe("kdive-live-worker@1.service", FakeDeadline(120.0))
    assert cast(UnitObservation, observation).membership == "unknown"
    path.rmdir()

    path.write_bytes(b"populated 0\n" + b"x" * 4096)
    observation = runtime.observe("kdive-live-worker@1.service", FakeDeadline(120.0))
    assert cast(UnitObservation, observation).membership == "unknown"


@pytest.mark.parametrize("boot_id", ["", "not-a-boot-id", _BOOT_ID + "suffix"])
def test_observe_rejects_missing_or_malformed_boot_id(
    fake_host: tuple[Path, Path], boot_id: str
) -> None:
    boot_id_path, _ = fake_host
    boot_id_path.write_text(boot_id, encoding="ascii")
    with pytest.raises(SystemdUnavailable, match="boot ID"):
        _runtime(fake_host, FakeRunner()).observe(
            "kdive-live-worker@1.service", FakeDeadline(120.0)
        )


def _write_process(
    proc_root: Path,
    pid: int,
    *,
    uid: int,
    cgroup: str,
    worker: bool = True,
    launcher: bytes = b"/opt/kdive/.venv/bin/python",
) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    argv = [launcher, b"-m", b"kdive", b"worker"]
    if not worker:
        argv[-1] = b"server"
    (process / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    (process / "status").write_text(f"Name:\tpython\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (process / "cgroup").write_text(f"0::{cgroup}\n")


def test_unmanaged_worker_scan_excludes_only_fixed_unit_cgroups(tmp_path: Path) -> None:
    _write_process(tmp_path, 77, uid=1000, cgroup="/user.slice/session-1.scope")
    _write_process(
        tmp_path,
        88,
        uid=1001,
        cgroup="/system.slice/kdive-live-worker@1.service",
    )
    _write_process(tmp_path, 99, uid=1002, cgroup="/user.slice/session-1.scope", worker=False)

    runtime = SystemdRuntime(FakeRunner(), proc_root=tmp_path)

    assert runtime.unmanaged_workers() == (UnmanagedWorker(pid=77, uid=1000),)


@pytest.mark.parametrize(
    "launcher",
    [
        b"python",
        b"python3",
        b"python3.14",
        b"/usr/bin/python",
        b"/usr/bin/python3",
        b"/usr/bin/python3.14",
    ],
)
def test_unmanaged_scan_recognizes_each_supported_python_launcher(
    tmp_path: Path, launcher: bytes
) -> None:
    _write_process(
        tmp_path,
        77,
        uid=1000,
        cgroup="/user.slice/session-1.scope",
        launcher=launcher,
    )

    assert SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers() == (
        UnmanagedWorker(pid=77, uid=1000),
    )


@pytest.mark.parametrize("launcher", [b"python", b"python3", b"python3.14"])
def test_unmanaged_scan_excludes_relative_launchers_in_fixed_cgroups(
    tmp_path: Path, launcher: bytes
) -> None:
    _write_process(
        tmp_path,
        77,
        uid=1000,
        cgroup="/system.slice/kdive-live-worker@1.service",
        launcher=launcher,
    )

    assert SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers() == ()


@pytest.mark.parametrize("launcher", [b"./python", b".venv/bin/python"])
def test_unmanaged_scan_recognizes_relative_python_paths_outside_fixed_cgroups(
    tmp_path: Path, launcher: bytes
) -> None:
    _write_process(
        tmp_path,
        77,
        uid=1000,
        cgroup="/user.slice/session-1.scope",
        launcher=launcher,
    )

    assert SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers() == (
        UnmanagedWorker(pid=77, uid=1000),
    )


@pytest.mark.parametrize("launcher", [b"./python", b".venv/bin/python"])
def test_unmanaged_scan_excludes_relative_python_paths_in_fixed_cgroups(
    tmp_path: Path, launcher: bytes
) -> None:
    _write_process(
        tmp_path,
        77,
        uid=1000,
        cgroup="/system.slice/kdive-live-worker@1.service",
        launcher=launcher,
    )

    assert SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers() == ()


@pytest.mark.parametrize(
    "launcher",
    [
        b"python3.13",
        b"python3.140",
        b"python-worker",
        b"xpython",
        b"/usr/bin/python3.13",
        b"/usr/bin/python-wrapper",
        b"./python-wrapper",
        b".venv/bin/notpython",
        b".venv/bin/python3.140",
    ],
)
def test_unmanaged_scan_rejects_lookalike_python_launchers(tmp_path: Path, launcher: bytes) -> None:
    _write_process(
        tmp_path,
        77,
        uid=1000,
        cgroup="/user.slice/session-1.scope",
        launcher=launcher,
    )

    assert SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers() == ()


@pytest.mark.parametrize(
    "cgroup",
    [
        "/system.slice/kdive-live-worker@9.service",
        "/system.slice/kdive-live-worker@1.service/foreign",
        "/user.slice/kdive-live-worker@1.service",
    ],
)
def test_unmanaged_scan_does_not_accept_lookalike_cgroups(tmp_path: Path, cgroup: str) -> None:
    _write_process(tmp_path, 77, uid=1000, cgroup=cgroup)
    assert SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers() == (
        UnmanagedWorker(pid=77, uid=1000),
    )


def test_unmanaged_scan_fails_closed_for_malformed_worker_cgroup(tmp_path: Path) -> None:
    _write_process(tmp_path, 77, uid=1000, cgroup="/user.slice/session-1.scope")
    (tmp_path / "77/cgroup").write_text("1:name=foreign:/x\n")
    with pytest.raises(SystemdConflict, match="cgroup"):
        SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers()


def test_unmanaged_scan_fails_closed_for_oversized_process_metadata(tmp_path: Path) -> None:
    _write_process(tmp_path, 77, uid=1000, cgroup="/user.slice/session-1.scope")
    (tmp_path / "77/cmdline").write_bytes(
        b"/opt/kdive/.venv/bin/python\0-m\0kdive\0worker\0" + b"x" * 4096
    )
    with pytest.raises(SystemdConflict, match="command"):
        SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers()


def test_unmanaged_scan_fails_closed_for_malformed_worker_candidate(tmp_path: Path) -> None:
    _write_process(tmp_path, 77, uid=1000, cgroup="/user.slice/session-1.scope")
    (tmp_path / "77/cmdline").write_bytes(b"/opt/kdive/.venv/bin/python\0-m\0kdive\0worker")
    with pytest.raises(SystemdConflict, match="command"):
        SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers()


def test_unmanaged_scan_ignores_long_argv_after_exact_non_worker_launcher(
    tmp_path: Path,
) -> None:
    _write_process(tmp_path, 77, uid=1000, cgroup="/user.slice/session-1.scope")
    (tmp_path / "77/cmdline").write_bytes(b"/usr/bin/java\0" + b"x" * 8192 + b"\0")

    assert SystemdRuntime(FakeRunner(), proc_root=tmp_path).unmanaged_workers() == ()


def test_signal_terminate_preserves_the_retained_unit() -> None:
    runner = FakeRunner()
    deadline = FakeDeadline(45.0)
    SystemdRuntime(runner).signal_terminate("kdive-live-worker@2.service", deadline=deadline)
    assert runner.calls == [
        (
            "systemctl",
            "kill",
            "--kill-whom=all",
            "--signal=SIGTERM",
            "kdive-live-worker@2.service",
        )
    ]
    assert runner.deadlines == [deadline]


def test_signal_terminate_cancels_and_reaps_blocking_child_at_shared_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline = MonotonicDeadline.after(1.0)
    runner = SubprocessCommandRunner(deadline)
    child: subprocess.Popen[bytes] | None = None
    program = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "print('ready', flush=True);"
        "time.sleep(60)"
    )

    def launch(_argv: Sequence[str]) -> subprocess.Popen[bytes]:
        nonlocal child
        child = subprocess.Popen(  # noqa: S603 - fixed test interpreter and program.
            (sys.executable, "-c", program),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
        )
        return child

    monkeypatch.setattr(SubprocessCommandRunner, "_launch", staticmethod(launch))
    with pytest.raises(CommandDeadlineExceeded):
        SystemdRuntime(runner).signal_terminate("kdive-live-worker@2.service", deadline=deadline)

    assert child is not None
    assert child.returncode == -signal.SIGKILL
    assert deadline.remaining() > 0


def test_require_inactive_accepts_only_empty_unit_identity() -> None:
    deadline = FakeDeadline(120.0)
    runner = FakeRunner(_INACTIVE_PROPERTIES)

    SystemdRuntime(runner).require_inactive("kdive-live-worker@2.service", deadline=deadline)

    assert runner.calls == [
        (
            "systemctl",
            "show",
            "--property=ActiveState,SubState,Result,ExecMainStatus,ControlGroup,InvocationID",
            "kdive-live-worker@2.service",
        )
    ]
    assert runner.deadlines == [deadline]


def test_require_inactive_rejects_retained_invocation() -> None:
    deadline = FakeDeadline(120.0)
    runner = FakeRunner()

    with pytest.raises(SystemdConflict, match="inactive"):
        SystemdRuntime(runner).require_inactive("kdive-live-worker@1.service", deadline=deadline)

    assert runner.deadlines == [deadline]


def test_stop_retained_is_a_separate_bounded_operation() -> None:
    runner = FakeRunner()
    deadline = FakeDeadline(120.0)
    SystemdRuntime(runner).stop_retained("kdive-live-worker@2.service", deadline=deadline)
    assert runner.calls == [("systemctl", "stop", "kdive-live-worker@2.service")]
    assert runner.deadlines == [deadline]


def test_start_and_reset_use_only_the_exact_fixed_unit() -> None:
    runner = FakeRunner()
    runtime = SystemdRuntime(runner)
    deadline = FakeDeadline(120.0)
    runtime.start("kdive-live-worker@2.service", deadline=deadline)
    runtime.reset("kdive-live-worker@2.service", deadline=deadline)
    assert runner.calls == [
        ("systemctl", "start", "kdive-live-worker@2.service"),
        ("systemctl", "reset-failed", "kdive-live-worker@2.service"),
    ]
    assert runner.deadlines == [deadline, deadline]


def test_runtime_rejects_caller_selected_or_special_unit_names() -> None:
    runner = FakeRunner()
    runtime = SystemdRuntime(runner)
    for unit in (
        "kdive-live-worker@0.service",
        "kdive-live-worker@9.service",
        "kdive-live-worker@1.service --now",
        "../kdive-live-worker@1.service",
    ):
        with pytest.raises(SystemdConflict, match="fixed worker unit"):
            runtime.start(unit, deadline=FakeDeadline(120.0))
    assert runner.calls == []


def test_journal_selects_only_the_exact_invocation() -> None:
    runner = FakeRunner("journal bytes")
    deadline = FakeDeadline(30.0)
    output = SystemdRuntime(runner).journal(_INVOCATION_ID, 320 * 1024, deadline)
    assert output == "journal bytes"
    assert runner.calls == [
        ("journalctl", "--no-pager", f"_SYSTEMD_INVOCATION_ID={_INVOCATION_ID}")
    ]
    assert runner.deadlines == [deadline]
    assert runner.byte_limits == [320 * 1024]
    assert runner.truncations == [True]


def test_journal_rejects_non_exact_invocation_before_running() -> None:
    runner = FakeRunner()
    with pytest.raises(SystemdConflict, match="invocation"):
        SystemdRuntime(runner).journal("a" * 31 + "Z", 1024, FakeDeadline(1.0))
    assert runner.calls == []


def test_real_runner_passes_argument_arrays_without_a_shell() -> None:
    token = "$(printf shell-expanded)"
    runner = SubprocessCommandRunner(MonotonicDeadline.after(5.0))
    output = runner.run(
        (sys.executable, "-c", "import sys; print(sys.argv[1], end='')", token),
        byte_limit=1024,
    )
    assert output == token


def test_real_runner_caps_bytes_before_replacement_decoding() -> None:
    runner = SubprocessCommandRunner(MonotonicDeadline.after(5.0))
    output = runner.run(
        (sys.executable, "-c", "import os; os.write(1, b'\\xffabcdef')"),
        byte_limit=4,
        allow_truncation=True,
    )
    assert output == "�abc"


def test_real_runner_rejects_truncated_control_output() -> None:
    runner = SubprocessCommandRunner(MonotonicDeadline.after(5.0))
    with pytest.raises(CommandOutputTooLarge):
        runner.run(
            (sys.executable, "-c", "print('x' * 128, end='')"),
            byte_limit=8,
        )


def test_real_runner_terminates_a_timed_out_child() -> None:
    program = "import signal,time;signal.signal(signal.SIGTERM, lambda *_: exit(0));time.sleep(60)"
    runner = SubprocessCommandRunner(MonotonicDeadline.after(1.0))
    with pytest.raises(CommandDeadlineExceeded):
        runner.run((sys.executable, "-c", program), byte_limit=1024)


def test_sigkill_cleanup_uses_only_the_exact_remaining_deadline_budget() -> None:
    process = UnreapableProcess()
    deadline = ScriptedDeadline((0.4, 0.1))

    with pytest.raises(CommandCleanupDeadlineExceeded):
        SubprocessCommandRunner._terminate(
            cast(subprocess.Popen[bytes], process),
            deadline,
        )

    assert process.calls == [
        "poll",
        "terminate",
        ("wait", 0.1),
        "kill",
        ("wait", 0.1),
    ]


def test_real_process_sigkills_and_reaps_a_child_that_ignores_sigterm() -> None:
    program = (
        "import os,signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "print(os.getpid(), flush=True);"
        "time.sleep(60)"
    )
    process = subprocess.Popen(  # noqa: S603 - fixed test interpreter and program.
        (sys.executable, "-c", program),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(10.0), "child must install SIGTERM ignore before cleanup"
        assert int(process.stdout.readline()) == process.pid

        started = time.monotonic()
        SubprocessCommandRunner._terminate(process, MonotonicDeadline.after(2.0))

        assert time.monotonic() - started < 3.0
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)


def test_monotonic_deadline_never_reports_negative_time() -> None:
    now = time.monotonic()
    deadline = MonotonicDeadline(expires_at=now - 1.0, monotonic=lambda: now)
    assert deadline.remaining() == 0.0


@pytest.mark.parametrize("seconds", [-1.0, float("nan"), float("inf")])
def test_monotonic_deadline_rejects_unbounded_durations(seconds: float) -> None:
    with pytest.raises(ValueError, match="finite non-negative"):
        MonotonicDeadline.after(seconds)
