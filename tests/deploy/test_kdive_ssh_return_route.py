"""Behavior of the remote SSH-forward return-route dispatcher script (ADR-0721).

The script runs in the guest as a NetworkManager dispatcher. ``ip`` is stubbed on ``PATH``: the stub
records every invocation and answers the two ``route show`` queries the ``down`` path reads from
files the test writes, so each case pins exactly which routing changes the script makes.

The role-task tests run the ``virt-customize --run-command`` text from ``build_one.yml`` with the
image paths rewritten into a temporary directory and ``install`` stubbed, so the install guard and
failure propagation are exercised rather than read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "remote-libvirt-guest-helpers" / "kdive-ssh-return-route"
BUILD_ONE = ROOT / "deploy" / "ansible" / "roles" / "guest_base_image" / "tasks" / "build_one.yml"
SH = shutil.which("sh")

_IP_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/calls"
case "$*" in
  "-4 route show table 2291 dev "*)
    cat "$STUB_DIR/route_dev" 2>/dev/null
    exit "$(cat "$STUB_DIR/route_dev_rc" 2>/dev/null || echo 0)" ;;
  "-4 route show table 2291")
    cat "$STUB_DIR/route_all" 2>/dev/null ;;
  "$IP_FAIL_ON")
    exit 2 ;;
esac
exit 0
"""

_SLIRP_ROUTE = "default via 10.0.2.2 dev ens16\n"
_INSTALL_CALLS = [
    "-4 route replace default via 10.0.2.2 dev ens16 table 2291",
    "-4 rule flush table 2291",
    "-4 rule add pref 2291 from 10.0.2.15 table 2291",
]


def _run(
    tmp_path: Path,
    iface: str,
    action: str,
    *,
    lease: str | None = None,
    route_dev: str = "",
    route_dev_rc: int = 0,
    route_all: str = "",
    fail_on: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    assert SH is not None, "sh is required to run the dispatcher script"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    ip = stub_bin / "ip"
    ip.write_text(_IP_STUB, encoding="utf-8")
    ip.chmod(0o755)
    (tmp_path / "route_dev").write_text(route_dev, encoding="utf-8")
    (tmp_path / "route_dev_rc").write_text(str(route_dev_rc), encoding="utf-8")
    (tmp_path / "route_all").write_text(route_all, encoding="utf-8")
    env = {
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        "STUB_DIR": str(tmp_path),
        "IP_FAIL_ON": fail_on or "never matches",
    }
    if lease is not None:
        env["DHCP4_IP_ADDRESS"] = lease
    proc = subprocess.run(
        [SH, str(SCRIPT), iface, action], env=env, capture_output=True, text=True, check=False
    )
    calls_file = tmp_path / "calls"
    calls = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
    return proc, calls


def test_up_with_slirp_lease_routes_the_lease_back_through_slirp(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "ens16", "up", lease="10.0.2.15")
    assert proc.returncode == 0, proc.stderr
    assert calls == _INSTALL_CALLS


@pytest.mark.parametrize("lease", ["192.168.122.40", "10.0.21.5", "110.0.2.15", "", None])
def test_up_outside_the_slirp_subnet_changes_nothing(tmp_path: Path, lease: str | None) -> None:
    proc, calls = _run(tmp_path, "ens2", "up", lease=lease)
    assert proc.returncode == 0, proc.stderr
    assert calls == []


def test_dhcp4_change_replaces_the_rule_for_the_new_lease(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "ens16", "dhcp4-change", lease="10.0.2.16")
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "-4 route replace default via 10.0.2.2 dev ens16 table 2291",
        "-4 rule flush table 2291",
        "-4 rule add pref 2291 from 10.0.2.16 table 2291",
    ]


def test_down_of_the_slirp_interface_removes_route_and_rule(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "ens16", "down", route_dev=_SLIRP_ROUTE, route_all=_SLIRP_ROUTE)
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "-4 route show table 2291 dev ens16",
        "-4 route flush table 2291",
        "-4 rule flush table 2291",
    ]


def test_down_of_another_interface_keeps_the_return_path(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "ens2", "down", route_all=_SLIRP_ROUTE)
    assert proc.returncode == 0, proc.stderr
    assert calls == ["-4 route show table 2291 dev ens2", "-4 route show table 2291"]


@pytest.mark.parametrize("route_dev_rc", [0, 1], ids=["route-dropped", "device-vanished"])
def test_down_after_the_kernel_dropped_the_route_removes_the_rule(
    tmp_path: Path, route_dev_rc: int
) -> None:
    proc, calls = _run(tmp_path, "ens16", "down", route_dev_rc=route_dev_rc)
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "-4 route show table 2291 dev ens16",
        "-4 route show table 2291",
        "-4 rule flush table 2291",
    ]


@pytest.mark.parametrize("action", ["pre-up", "connectivity-change", "hostname", ""])
def test_other_actions_change_nothing(tmp_path: Path, action: str) -> None:
    proc, calls = _run(tmp_path, "ens16", action, lease="10.0.2.15")
    assert proc.returncode == 0, proc.stderr
    assert calls == []


@pytest.mark.parametrize("failing", range(len(_INSTALL_CALLS)))
def test_a_failed_routing_change_stops_and_fails_the_script(tmp_path: Path, failing: int) -> None:
    proc, calls = _run(tmp_path, "ens16", "up", lease="10.0.2.15", fail_on=_INSTALL_CALLS[failing])
    assert proc.returncode != 0
    assert calls == _INSTALL_CALLS[: failing + 1]


def _install_task() -> dict[str, Any]:
    tasks: list[dict[str, Any]] = yaml.safe_load(BUILD_ONE.read_text(encoding="utf-8"))
    return next(task for task in tasks if "return-route dispatcher" in task["name"])


def _install_command(image_root: Path) -> str:
    argv: list[str] = _install_task()["ansible.builtin.command"]["argv"]
    command = argv[argv.index("--run-command") + 1]
    return command.replace("/tmp/", f"{image_root}/tmp/").replace(
        "/etc/NetworkManager/", f"{image_root}/etc/NetworkManager/"
    )


def test_role_uploads_the_staged_script() -> None:
    argv: list[str] = _install_task()["ansible.builtin.command"]["argv"]
    assert argv[argv.index("--upload") + 1] == (
        "{{ guest_base_image_helper_dir }}/kdive-ssh-return-route:/tmp/kdive-ssh-return-route"
    )


def _run_install(tmp_path: Path, *, with_dispatcher: bool, install_rc: int = 0) -> tuple[int, Path]:
    image_root = tmp_path / "image"
    (image_root / "tmp").mkdir(parents=True)
    (image_root / "tmp" / "kdive-ssh-return-route").write_text("script", encoding="utf-8")
    if with_dispatcher:
        (image_root / "etc" / "NetworkManager" / "dispatcher.d").mkdir(parents=True)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    record = tmp_path / "install.args"
    install = stub_bin / "install"
    install.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {record}\nexit {install_rc}\n", encoding="utf-8"
    )
    install.chmod(0o755)
    restorecon = stub_bin / "restorecon"
    restorecon.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path / 'restorecon.args'}\n", encoding="utf-8"
    )
    restorecon.chmod(0o755)
    rm = shutil.which("rm")
    assert SH is not None and rm is not None
    (stub_bin / "rm").symlink_to(rm)
    env = {**os.environ, "PATH": str(stub_bin)}
    proc = subprocess.run([SH, "-c", _install_command(image_root)], env=env, check=False)
    return proc.returncode, record


def test_role_installs_into_the_dispatcher_directory_as_root(tmp_path: Path) -> None:
    returncode, record = _run_install(tmp_path, with_dispatcher=True)
    image_root = tmp_path / "image"
    assert returncode == 0
    assert record.read_text(encoding="utf-8").splitlines() == [
        "-o",
        "root",
        "-g",
        "root",
        "-m",
        "0755",
        f"{image_root}/tmp/kdive-ssh-return-route",
        f"{image_root}/etc/NetworkManager/dispatcher.d/50-kdive-ssh-return-route",
    ]
    assert (tmp_path / "restorecon.args").read_text(encoding="utf-8").splitlines() == [
        "-v",
        f"{image_root}/etc/NetworkManager/dispatcher.d/50-kdive-ssh-return-route",
    ]
    assert not (image_root / "tmp" / "kdive-ssh-return-route").exists()


def test_role_skips_an_image_without_networkmanager(tmp_path: Path) -> None:
    returncode, record = _run_install(tmp_path, with_dispatcher=False)
    assert returncode == 0
    assert not record.exists()
    assert not (tmp_path / "restorecon.args").exists()
    assert not (tmp_path / "image" / "tmp" / "kdive-ssh-return-route").exists()


def test_role_install_failure_fails_the_build(tmp_path: Path) -> None:
    returncode, _ = _run_install(tmp_path, with_dispatcher=True, install_rc=1)
    assert returncode != 0
