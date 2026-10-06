"""Read-only lane prerequisites for native and emulated live VM preparation."""

from __future__ import annotations

import errno
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import live_vm_lane as lane


def _caps(
    host: str = "x86_64",
    guest: str = "x86_64",
    accel: str = "kvm",
    emulator: str = "/usr/bin/qemu-system-x86_64",
) -> str:
    return (
        f"<capabilities><host><cpu><arch>{host}</arch></cpu></host>"
        f"<guest><os_type>hvm</os_type><arch name='{guest}'>"
        f"<emulator>{emulator}</emulator><domain type='{accel}'/>"
        "</arch></guest></capabilities>"
    )


def _machine(monkeypatch: pytest.MonkeyPatch, host: str, xml: str) -> list[list[str]]:
    commands: list[list[str]] = []
    monkeypatch.setattr(lane.platform, "machine", lambda: host)

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(argv)
        assert isinstance(kwargs["timeout"], int) and kwargs["timeout"] > 0
        assert kwargs["check"] is True
        return SimpleNamespace(stdout=xml)

    monkeypatch.setattr(lane.subprocess, "run", run)
    return commands


def _kvm(
    monkeypatch: pytest.MonkeyPatch, *, hv: bool = True, api: int = 12
) -> tuple[list[tuple[int, int, int]], list[int]]:
    calls: list[tuple[int, int, int]] = []
    closed: list[int] = []
    monkeypatch.setattr(lane.os, "open", lambda path, flags: 40)
    monkeypatch.setattr(lane.os, "close", closed.append)

    def ioctl(fd: int, command: int, arg: int = 0) -> int:
        calls.append((fd, command, arg))
        if command & 0xFF == 1:
            if not hv:
                raise OSError(errno.EINVAL, "KVM-HV unavailable")
            return 41
        return api

    monkeypatch.setattr(lane.fcntl, "ioctl", ioctl)
    return calls, closed


@pytest.mark.parametrize(
    "action,host,guest,accel,reason",
    [
        ("native-x86", "ppc64le", "x86_64", "kvm", "host"),
        ("native-x86", "x86_64", "ppc64le", "kvm", "x86_64"),
        ("native-x86", "x86_64", "x86_64", "tcg", "KVM"),
        ("native-power", "x86_64", "ppc64le", "kvm", "host"),
        ("tcg-host", "ppc64le", "ppc64le", "tcg", "x86_64"),
        ("tcg-host", "x86_64", "ppc64le", "kvm", "TCG"),
    ],
)
def test_lane_rejects_wrong_arch_or_accel(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    host: str,
    guest: str,
    accel: str,
    reason: str,
) -> None:
    _machine(monkeypatch, host, _caps(host, guest, accel))
    with pytest.raises(lane.PreflightError, match=reason):
        lane.check_lane(action, "qemu:///session")


@pytest.mark.parametrize(
    "uri",
    [
        "qemu+ssh://remote/system",
        "qemu://remote/system",
        "qemu:///other",
        "qemu+tcp://remote/system",
        "qemu+unix://remote/system",
        "qemu:///system?host=remote",
        "qemu:///system?host=",
        "qemu+unix:///session?socket=",
    ],
)
def test_remote_or_unexpected_uri_rejected_before_virsh(
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
) -> None:
    commands = _machine(monkeypatch, "x86_64", _caps())
    with pytest.raises(lane.PreflightError, match="local"):
        lane.check_lane("native-x86", uri)
    assert commands == []


def test_native_x86_kvm_api_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _machine(monkeypatch, "x86_64", _caps())
    calls, closed = _kvm(monkeypatch)
    lane.check_lane("native-x86", "qemu:///session")
    assert commands == [["virsh", "-c", "qemu:///session", "capabilities"]]
    assert calls == [(40, 0xAE00, 0)]
    assert closed == [40]


def test_capabilities_host_must_match_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    _machine(monkeypatch, "x86_64", _caps(host="ppc64le"))
    with pytest.raises(lane.PreflightError, match="disagrees"):
        lane.check_lane("native-x86", "qemu:///session")


@pytest.mark.parametrize(
    "hv,api,expected",
    [
        (True, 12, [(40, 0x2000AE00, 0), (40, 0x2000AE01, 1)]),
        (False, 12, [(40, 0x2000AE00, 0), (40, 0x2000AE01, 1)]),
        (True, 11, [(40, 0x2000AE00, 0)]),
    ],
)
def test_native_power_ioctl_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    hv: bool,
    api: int,
    expected: list[tuple[int, int, int]],
) -> None:
    _machine(monkeypatch, "ppc64le", _caps("ppc64le", "ppc64le"))
    calls, closed = _kvm(monkeypatch, hv=hv, api=api)
    if not hv or api != 12:
        with pytest.raises(lane.PreflightError, match="KVM"):
            lane.check_lane("native-power", "qemu:///system")
    else:
        lane.check_lane("native-power", "qemu:///system")
    assert calls == expected
    assert closed == ([41, 40] if hv and api == 12 else [40])


def test_kvm_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _machine(monkeypatch, "x86_64", _caps())

    def unavailable(path: str, flags: int) -> int:
        raise PermissionError("/dev/kvm")

    monkeypatch.setattr(lane.os, "open", unavailable)
    with pytest.raises(lane.PreflightError, match="/dev/kvm"):
        lane.check_lane("native-x86", "qemu:///session")


@pytest.mark.parametrize("xml", ["<broken", "<capabilities/>"])
def test_bad_capability_xml(monkeypatch: pytest.MonkeyPatch, xml: str) -> None:
    _machine(monkeypatch, "x86_64", xml)
    with pytest.raises(lane.PreflightError, match="capabilities"):
        lane.check_lane("native-x86", "qemu:///session")


def test_virsh_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lane.platform, "machine", lambda: "x86_64")

    def timeout(argv: list[str], **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(lane.subprocess, "run", timeout)
    with pytest.raises(lane.PreflightError, match="virsh"):
        lane.check_lane("native-x86", "qemu:///session")


@pytest.mark.parametrize("executable", [True, False])
def test_tcg_uses_reported_emulator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    executable: bool,
) -> None:
    emulator = tmp_path / "qemu-system-ppc64le"
    emulator.write_text("#!/bin/sh\n")
    emulator.chmod(0o755 if executable else 0o644)
    _machine(monkeypatch, "x86_64", _caps("x86_64", "ppc64le", "tcg", str(emulator)))
    if executable:
        lane.check_lane("tcg-host", "qemu+unix:///session")
        lane.check_lane("tcg-host", "qemu+unix:///session?socket=/tmp/libvirt.sock")
    else:
        with pytest.raises(lane.PreflightError, match="emulator"):
            lane.check_lane("tcg-host", "qemu:///session")


def test_tcg_missing_emulator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "qemu-system-ppc64le"
    _machine(monkeypatch, "x86_64", _caps("x86_64", "ppc64le", "tcg", str(missing)))
    with pytest.raises(lane.PreflightError, match="emulator"):
        lane.check_lane("tcg-host", "qemu:///session")


def _capacity_os(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cpus: int = 8,
    available_kib: int = 16 * 1024 * 1024,
    free: int = 48 * 1024**3,
) -> None:
    monkeypatch.setattr(lane.os, "sched_getaffinity", lambda pid: set(range(cpus)), raising=False)
    monkeypatch.setattr(lane.Path, "read_text", lambda path: f"MemAvailable: {available_kib} kB\n")
    monkeypatch.setattr(lane.shutil, "disk_usage", lambda path: SimpleNamespace(free=free))


def test_capacity_exact_threshold_and_retained_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _capacity_os(monkeypatch)
    lane.check_capacity(tmp_path, "8", "16384", "51539607552")


@pytest.mark.parametrize(
    "cpus,memory,free,reason",
    [
        (7, 16 * 1024 * 1024, 48 * 1024**3, "CPU"),
        (8, 16 * 1024 * 1024 - 1, 48 * 1024**3, "memory"),
        (8, 16 * 1024 * 1024, 48 * 1024**3 - 1, "disk"),
    ],
)
def test_capacity_shortage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cpus: int,
    memory: int,
    free: int,
    reason: str,
) -> None:
    _capacity_os(monkeypatch, cpus=cpus, available_kib=memory, free=free)
    with pytest.raises(lane.PreflightError, match=reason):
        lane.check_capacity(tmp_path, "8", "16384", "51539607552")


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "junk", "", "9" * 21])
def test_capacity_bad_input_before_os_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    value: str,
) -> None:
    def unexpected(pid: int) -> set[int]:
        raise AssertionError("OS probe happened")

    monkeypatch.setattr(lane.os, "sched_getaffinity", unexpected, raising=False)
    with pytest.raises(lane.PreflightError):
        lane.check_capacity(tmp_path, value, "16384", "51539607552")


@pytest.mark.parametrize("field", ["memory", "disk"])
def test_other_capacity_inputs_rejected_before_os_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    def unexpected(pid: int) -> set[int]:
        raise AssertionError("OS probe happened")

    monkeypatch.setattr(lane.os, "sched_getaffinity", unexpected, raising=False)
    memory, disk = ("0", "51539607552") if field == "memory" else ("16384", "-1")
    with pytest.raises(lane.PreflightError):
        lane.check_capacity(tmp_path, "8", memory, disk)


def test_capacity_missing_path_and_unreadable_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(lane.PreflightError, match="workspace"):
        lane.check_capacity(tmp_path / "absent", "8", "16384", "51539607552")
    _capacity_os(monkeypatch)
    monkeypatch.setattr(lane.Path, "read_text", lambda path: "MemTotal: 100 kB\n")
    with pytest.raises(lane.PreflightError, match="MemAvailable"):
        lane.check_capacity(tmp_path, "8", "16384", "51539607552")
