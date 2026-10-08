"""Native networkd events preserve the Ubuntu remote SSH and ordinary egress paths."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "deploy/remote-libvirt-guest-helpers"
ADAPTER = ASSETS / "kdive-networkd-ssh-return-route"


def _run(
    tmp_path: Path,
    *,
    state: str = "configured",
    addresses: str = "10.0.2.15",
    iface: str = "ens16",
    default: str = "default via 10.0.2.2 dev ens16 proto dhcp",
    fail: str = "",
    owned: str = "default via 10.0.2.2 dev ens16",
):
    script = tmp_path / "adapter"
    script.write_text(
        ADAPTER.read_text().replace(
            "/usr/local/libexec/kdive-ssh-return-route", str(ASSETS / "kdive-ssh-return-route")
        )
    )
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    ip = binary / "ip"
    ip.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$RECORD"
[ "$*" != "$FAIL" ] || exit 23
case "$*" in
  "-4 route show table main default proto dhcp via 10.0.2.2 dev "*) printf '%s\\n' "$DEFAULT" ;;
  "-4 route show table 2291 dev ens16") printf '%s\\n' "$OWNED" ;;
  "-4 route show table 2291 dev "*) ;;
  "-4 route show table 2291") printf '%s\\n' "$OWNED" ;;
esac
""")
    ip.chmod(0o755)
    record = tmp_path / "calls"
    record.write_text("")
    result = subprocess.run(
        ["/bin/sh", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{binary}:/usr/bin:/bin",
            "IFACE": iface,
            "STATE": state,
            "IP_ADDRS": addresses,
            "RECORD": str(record),
            "DEFAULT": default,
            "FAIL": fail,
            "OWNED": owned,
        },
        check=False,
    )
    return result, record.read_text().splitlines()


QUERY = "-4 route show table main default proto dhcp via 10.0.2.2 dev ens16"
DELETE = "-4 route del default proto dhcp via 10.0.2.2 dev ens16 table main"
INSTALL = [
    "-4 route replace default via 10.0.2.2 dev ens16 table 2291",
    "-4 rule flush table 2291",
    "-4 rule add pref 2291 from 10.0.2.15 table 2291",
]


@pytest.mark.parametrize("state", ["configured", "routable"])
def test_slirp_event_removes_only_identified_dhcp_default_then_installs(tmp_path, state):
    proc, calls = _run(tmp_path, state=state, addresses="192.0.2.8 10.0.2.15")
    assert proc.returncode == 0, proc.stderr
    assert calls == [QUERY, DELETE, *INSTALL]


def test_absent_dhcp_default_does_not_delete_static_or_other_routes(tmp_path):
    proc, calls = _run(tmp_path, default="")
    assert proc.returncode == 0, proc.stderr
    assert calls == [QUERY, *INSTALL]


def test_repeat_and_replacement_lease_reapply_source_policy(tmp_path):
    for lease in ["10.0.2.15", "10.0.2.15", "10.0.2.16"]:
        proc, calls = _run(tmp_path, addresses=lease)
        assert proc.returncode == 0, proc.stderr
        assert calls[-1] == f"-4 rule add pref 2291 from {lease} table 2291"


@pytest.mark.parametrize("state", ["off", "no-carrier", "failed", "linger"])
def test_lost_slirp_link_removes_owned_policy(tmp_path, state):
    proc, calls = _run(tmp_path, state=state)
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "-4 route show table 2291 dev ens16",
        "-4 route flush table 2291",
        "-4 rule flush table 2291",
    ]


def test_primary_event_does_not_mutate_slirp_policy(tmp_path):
    proc, calls = _run(tmp_path, iface="ens2", addresses="192.0.2.8")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["-4 route show table 2291 dev ens2", "-4 route show table 2291"]


def test_lost_slirp_address_cleans_old_policy(tmp_path):
    proc, calls = _run(tmp_path, addresses="192.0.2.8")
    assert proc.returncode == 0, proc.stderr
    assert calls[-2:] == ["-4 route flush table 2291", "-4 rule flush table 2291"]


@pytest.mark.parametrize(
    "addresses", ["10.0.2.15 10.0.2.16", "10.0.2.999", "10.0.2.15;touch /tmp/no"]
)
def test_ambiguous_or_malformed_slirp_lease_fails_before_mutation(tmp_path, addresses):
    proc, calls = _run(tmp_path, addresses=addresses)
    assert proc.returncode != 0
    assert calls == []


@pytest.mark.parametrize("state", ["configuring", "carrier", "unmanaged", ""])
def test_irrelevant_event_is_noop(tmp_path, state):
    proc, calls = _run(tmp_path, state=state)
    assert proc.returncode == 0
    assert calls == []


@pytest.mark.parametrize("fail", [QUERY, DELETE, *INSTALL])
def test_native_failure_propagates_and_stops_later_mutation(tmp_path, fail):
    proc, calls = _run(tmp_path, fail=fail)
    assert proc.returncode == 23
    assert calls[-1] == fail


def _prepare_image(tmp_path: Path, *, foreign: bool = False, fail: str = ""):
    import re
    import shutil

    import yaml

    root = tmp_path / "image"
    (root / "tmp").mkdir(parents=True)
    (root / "etc/netplan").mkdir(parents=True)
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/lib/systemd/system").mkdir(parents=True)
    (root / "usr/lib/systemd/system/ssh.service").write_text(
        "[Service]\nExecStartPre=/usr/sbin/sshd -t\n"
    )
    (root / "usr/bin/networkd-dispatcher").write_text("native")
    (root / "usr/bin/networkd-dispatcher").chmod(0o755)
    if foreign:
        (root / "etc/netplan/50-operator.yaml").write_text("operator-owned")
    for name in [
        "kdive-ssh-return-route",
        "kdive-networkd-ssh-return-route",
        "kdive-ubuntu-network.yaml",
        "kdive-networkd-startup.conf",
        "kdive-ubuntu-ssh-host-keys.conf",
    ]:
        shutil.copyfile(ASSETS / name, root / "tmp" / name)
    tasks = yaml.safe_load(
        (ROOT / "deploy/ansible/roles/guest_base_image/tasks/build_one.yml").read_text()
    )
    task = next(t for t in tasks if "Ubuntu native network policy" in t["name"])
    assert "image.distro == 'ubuntu'" in task["when"]
    argv = task["ansible.builtin.command"]["argv"]
    command = argv[argv.index("--run-command") + 1]
    command = re.sub(
        r"/(?:tmp/|etc/|run/|usr/local/|usr/lib/|usr/bin/networkd-dispatcher)",
        lambda m: str(root) + m.group(),
        command,
    )
    binary = tmp_path / "commands"
    binary.mkdir()
    for name in ["netplan", "systemctl", "ip", "ssh-keygen"]:
        script = binary / name
        script.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "$RECORD"\n'
            f'[ "$FAIL" != "{name}" ] || exit 29\n'
        )
        script.chmod(0o755)
    real_install = shutil.which("install")
    assert real_install
    (binary / "install").write_text(
        """#!/bin/sh
printf '%s\\n' "install $*" >> "$RECORD"
[ "$FAIL" != install ] || exit 29
if [ "$1" = -d ]; then
  shift; shift; shift; shift; shift
  exec """
        + real_install
        + """ -d "$@"
fi
shift; shift; shift; shift
exec """
        + real_install
        + """ "$@"
"""
    )
    (binary / "install").chmod(0o755)
    record = tmp_path / "prepare.calls"
    env = {**os.environ, "PATH": f"{binary}:/usr/bin:/bin", "RECORD": str(record), "FAIL": fail}
    return root, command, env, record


def test_role_prepares_owned_native_policy_and_repeats(tmp_path):
    import shutil

    root, command, env, record = _prepare_image(tmp_path)
    for _ in range(2):
        for source in ASSETS.glob("kdive-*"):
            if source.is_file():
                shutil.copyfile(source, root / "tmp" / source.name)
        proc = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    assert (root / "etc/netplan/60-kdive.yaml").stat().st_mode & 0o777 == 0o600
    for state in ["configured", "routable", "off", "no-carrier", "failed", "linger"]:
        hook = root / f"etc/networkd-dispatcher/{state}.d/50-kdive-ssh-return-route"
        assert hook.resolve() == root / "usr/local/libexec/kdive-networkd-ssh-return-route"
        assert hook.stat().st_mode & 0o777 == 0o755
    dropin = root / "etc/systemd/system/networkd-dispatcher.service.d/50-kdive-startup.conf"
    assert dropin.read_text() == (ASSETS / "kdive-networkd-startup.conf").read_text()
    assert "$networkd_dispatcher_args --run-startup-triggers" in dropin.read_text()
    assert (
        "systemctl enable systemd-networkd.service networkd-dispatcher.service"
        in record.read_text()
    )
    assert "install -o root -g root -m 0755" in record.read_text()
    assert (
        root / "etc/cloud/cloud.cfg.d/99-kdive-network-policy.cfg"
    ).read_text() == "network: {config: disabled}\n"


def test_role_refuses_foreign_policy_without_changing_it(tmp_path):
    root, command, env, _ = _prepare_image(tmp_path, foreign=True)
    proc = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True)
    assert proc.returncode != 0 and "foreign netplan policy" in proc.stderr
    assert (root / "etc/netplan/50-operator.yaml").read_text() == "operator-owned"
    assert not (root / "etc/netplan/60-kdive.yaml").exists()


@pytest.mark.parametrize("fail", ["install", "netplan", "systemctl"])
def test_role_propagates_native_preparation_failure(tmp_path, fail):
    _, command, env, _ = _prepare_image(tmp_path, fail=fail)
    proc = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True)
    assert proc.returncode == 29


def test_role_installs_per_guest_hostkeys_without_running_keygen_in_image(tmp_path):
    root, command, env, record = _prepare_image(tmp_path)
    import shutil

    for source in ASSETS.glob("kdive-*"):
        if source.is_file():
            shutil.copyfile(source, root / "tmp" / source.name)
    proc = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    dropin = root / "etc/systemd/system/ssh.service.d/50-kdive-host-keys.conf"
    assert dropin.stat().st_mode & 0o777 == 0o644
    assert dropin.read_text().splitlines() == [
        "[Service]",
        "ExecStartPre=",
        "ExecStartPre=/usr/bin/ssh-keygen -A",
        "ExecStartPre=/usr/sbin/sshd -t",
    ]
    assert not any(line.startswith("ssh-keygen ") for line in record.read_text().splitlines())


@pytest.mark.parametrize("foreign", ["extra", "continued", "spaced", "dropin", "owned-changed"])
def test_role_refuses_unknown_ssh_prechecks_before_override(tmp_path, foreign):
    root, command, env, _ = _prepare_image(tmp_path)
    unit = root / "usr/lib/systemd/system/ssh.service"
    if foreign == "extra":
        unit.write_text(unit.read_text() + "ExecStartPre=/usr/bin/operator-check\n")
    elif foreign == "continued":
        unit.write_text("[Service]\nExecStartPre=/usr/sbin/sshd -t \\\n -f /operator.conf\n")
    elif foreign == "spaced":
        unit.write_text(unit.read_text() + "  ExecStartPre = /usr/bin/operator-check\n")
    else:
        target = root / "etc/systemd/system/ssh.service.d"
        target.mkdir(parents=True)
        name = "50-kdive-host-keys.conf" if foreign == "owned-changed" else "operator.conf"
        (target / name).write_text("[Service]\nExecStartPre=/usr/bin/operator-check\n")
    proc = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "SSH" in proc.stderr
    target = root / "etc/systemd/system/ssh.service.d/50-kdive-host-keys.conf"
    assert not target.exists() or "operator-check" in target.read_text()


def test_native_hostkeys_are_unique_per_clone_and_preserved_on_repeat(tmp_path):
    import hashlib

    fingerprints = []
    for name in ("clone-a", "clone-b"):
        root = tmp_path / name
        keys = root / "etc/ssh"
        keys.mkdir(parents=True)
        first = {}
        for _ in range(2):
            subprocess.run(["ssh-keygen", "-A", "-f", str(root)], check=True, capture_output=True)
            current = {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in keys.glob("ssh_host_*")
            }
            assert current
            if _:
                assert current == first
            first = current
        fingerprints.append(first)
    assert fingerprints[0].keys() == fingerprints[1].keys()
    assert all(fingerprints[0][key] != fingerprints[1][key] for key in fingerprints[0])


@pytest.mark.parametrize("fail", [False, True])
def test_key_generation_precedes_validation_and_failure_stops_start(tmp_path, fail):
    commands = [
        line.removeprefix("ExecStartPre=")
        for line in (ASSETS / "kdive-ubuntu-ssh-host-keys.conf").read_text().splitlines()
        if line.startswith("ExecStartPre=") and line != "ExecStartPre="
    ]
    record = tmp_path / "calls"
    for name in ("ssh-keygen", "sshd"):
        script = tmp_path / name
        script.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "$RECORD"\n'
            f"exit {31 if fail and name == 'ssh-keygen' else 0}\n"
        )
        script.chmod(0o755)
    result = None
    for command in commands:
        binary, *args = command.split()
        result = subprocess.run(
            [str(tmp_path / Path(binary).name), *args],
            env={**os.environ, "RECORD": str(record)},
            check=False,
        )
        if result.returncode:
            break  # systemd does not execute later prechecks after a failed ExecStartPre.
    assert result is not None and result.returncode == (31 if fail else 0)
    assert record.read_text().splitlines() == (
        ["ssh-keygen -A"] if fail else ["ssh-keygen -A", "sshd -t"]
    )
