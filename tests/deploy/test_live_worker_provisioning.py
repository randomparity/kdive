"""Structural proofs for the fixed live-worker host provisioning contract."""

from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deploy" / "systemd"
ROLE = ROOT / "deploy" / "ansible" / "roles" / "live_vm_host"
INSTALLER = SYSTEMD / "install-live-worker-lifecycle.sh"
MAIN_TASKS = ROLE / "tasks" / "main.yml"
VERIFY_TASKS = ROLE / "tasks" / "verify.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _yaml(path: Path) -> dict[str, object]:
    document: object = yaml.safe_load(_text(path))
    if not isinstance(document, dict):
        raise TypeError(f"expected YAML mapping in {path}")
    return cast(dict[str, object], document)


def test_fixed_slot_accounts_and_groups_are_declared() -> None:
    defaults = _yaml(DEFAULTS)
    assert defaults["live_vm_host_worker_accounts"] == [
        f"kdive-worker-{slot}" for slot in range(1, 9)
    ]
    assert defaults["live_vm_host_worker_control_group"] == "kdive-live-control"
    assert defaults["live_vm_host_worker_libvirt_group"] == "kdive-live-libvirt"
    assert defaults["live_vm_host_worker_libvirt_uri"] == (
        "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"
    )
    witness_dsn = defaults["live_vm_host_worker_witness_dsn"]
    assert isinstance(witness_dsn, str)
    assert "kdive-witness-member:kdive-witness-local" in witness_dsn


def test_ansible_uses_declarative_account_and_file_modules() -> None:
    tasks = _text(MAIN_TASKS)
    for module in (
        "ansible.builtin.group:",
        "ansible.builtin.user:",
        "ansible.builtin.file:",
        "ansible.builtin.copy:",
        "ansible.builtin.systemd_service:",
    ):
        assert module in tasks
    assert "live_vm_host_worker_accounts" in tasks
    assert 'groups: ["{{ live_vm_host_worker_libvirt_group }}"]' in tasks
    assert "groups: [sudo" not in tasks
    assert "groups: [docker" not in tasks


def test_ansible_installs_witness_venv_in_clean_host_order() -> None:
    tasks = _text(MAIN_TASKS)
    commands = re.sub(r"\s+", " ", tasks)
    create = (
        "{{ live_vm_host_uv_bin }} venv --python /usr/bin/python3 "
        "/opt/kdive-live-worker-lifecycle/.venv"
    )
    install = (
        "{{ live_vm_host_uv_bin }} pip install --python "
        "/opt/kdive-live-worker-lifecycle/.venv/bin/python /opt/kdive"
    )
    assert commands.index(create) < commands.index(install)
    assert "path: /opt/kdive-live-worker-lifecycle" in tasks
    assert "owner: root" in tasks
    assert "group: root" in tasks
    assert 'mode: "0755"' in tasks
    assert "dest: /opt/kdive-live-worker-lifecycle/revision" in tasks
    assert 'mode: "0444"' in tasks


def test_installer_reads_dsn_from_stdin_and_pins_install_order() -> None:
    source = _text(INSTALLER)
    assert source.startswith("#!/bin/bash\nset -euo pipefail\n")
    assert "IFS= read -r witness_dsn" in source
    assert "--witness-dsn" not in source
    create = "uv venv --python /usr/bin/python3 /opt/kdive-live-worker-lifecycle/.venv"
    install = "uv pip install --python /opt/kdive-live-worker-lifecycle/.venv/bin/python /opt/kdive"
    assert source.index(create) < source.index(install)
    assert "-m 0600" in source
    assert "/etc/kdive/credentials/live-worker-witness.dsn" in source
    assert "/opt/kdive-live-worker-lifecycle/revision" in source


def test_installer_is_an_executable_host_contract() -> None:
    assert INSTALLER.stat().st_mode & stat.S_IXUSR


def test_ansible_creates_shared_slots_parent_before_private_children() -> None:
    tasks = _text(MAIN_TASKS)
    parent_start = tasks.index("- name: Create the shared slots parent")
    child_start = tasks.index("- name: Create root-owned fixed slot directories")
    assert parent_start < child_start
    parent = tasks[parent_start:child_start]
    assert 'path: "{{ live_vm_host_worker_state_root }}/slots"' in parent
    assert "owner: root" in parent
    assert "group: root" in parent
    assert 'mode: "0755"' in parent


def _exercise_source_link_helper(
    tmp_path: Path, *, preexisting: bool
) -> subprocess.CompletedProcess:
    source = tmp_path / "source"
    source.mkdir()
    link = tmp_path / "kdive"
    if preexisting:
        link.symlink_to(source, target_is_directory=True)
    command = r"""
source "$1"
_prepare_source_link "$2" "$3"
[[ -L "$3" ]]
_cleanup_source_link
if [[ "$4" == preexisting ]]; then
  [[ -L "$3" ]]
else
  [[ ! -e "$3" && ! -L "$3" ]]
fi
"""
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            command,
            "bash",
            str(INSTALLER),
            str(source),
            str(link),
            "preexisting" if preexisting else "created",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_installer_preserves_preexisting_matching_source_link(tmp_path: Path) -> None:
    result = _exercise_source_link_helper(tmp_path, preexisting=True)
    assert result.returncode == 0, result.stderr


def test_installer_removes_only_source_link_created_by_this_run(tmp_path: Path) -> None:
    result = _exercise_source_link_helper(tmp_path, preexisting=False)
    assert result.returncode == 0, result.stderr


def _select_libvirt_tuple(
    tmp_path: Path, *, os_release: str, binaries: tuple[str, ...]
) -> subprocess.CompletedProcess:
    release = tmp_path / "os-release"
    release.write_text(os_release, encoding="utf-8")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    for binary in binaries:
        path = binary_dir / binary
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    command = r"""
source "$1"
PATH="$2"
_select_libvirt_tuple "$3"
printf '%s\n' "$_libvirt_daemon" "$_libvirt_config" "$_libvirt_socket" \
  "$_libvirt_pid" "$_libvirt_uri"
"""
    return subprocess.run(
        ["/bin/bash", "-c", command, "bash", str(INSTALLER), str(binary_dir), str(release)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_installer_selects_debian_monolithic_tuple(tmp_path: Path) -> None:
    result = _select_libvirt_tuple(
        tmp_path, os_release='ID=ubuntu\nID_LIKE="debian"\n', binaries=("libvirtd",)
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "libvirtd",
        "libvirtd-live.conf",
        "libvirt-sock",
        "libvirtd.pid",
        "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock",
    ]


def test_installer_selects_redhat_modular_tuple(tmp_path: Path) -> None:
    result = _select_libvirt_tuple(
        tmp_path, os_release='ID=fedora\nID_LIKE="rhel fedora"\n', binaries=("virtqemud",)
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "virtqemud",
        "virtqemud-live.conf",
        "virtqemud-sock",
        "virtqemud.pid",
        "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock",
    ]


def test_installer_rejects_unsupported_distro_family(tmp_path: Path) -> None:
    result = _select_libvirt_tuple(tmp_path, os_release="ID=arch\n", binaries=())
    assert result.returncode != 0
    assert "unsupported distro family" in result.stderr


def test_installer_rejects_missing_selected_daemon(tmp_path: Path) -> None:
    result = _select_libvirt_tuple(tmp_path, os_release="ID=debian\nID_LIKE=debian\n", binaries=())
    assert result.returncode != 0
    assert "selected Debian-family libvirtd executable is missing" in result.stderr


def test_installer_and_ansible_install_the_same_fixed_files() -> None:
    installer = _text(INSTALLER)
    tasks = _text(MAIN_TASKS)
    names = (
        "kdive-live-worker-gate",
        "kdive-live-worker-lifecycle",
        "kdive-live-worker@.service",
        "kdive-live-worker-lifecycle.socket",
        "kdive-live-worker-lifecycle@.service",
        "libvirtd-live.conf",
        "live-worker-lifecycle.conf",
    )
    for name in names:
        assert name in installer
        assert name in tasks
    for destination in ("/usr/local/libexec/", "/etc/systemd/system/", "/etc/kdive/"):
        assert destination in installer
        assert destination in tasks


def test_installer_and_ansible_pin_equivalent_authority_modes() -> None:
    installer = _text(INSTALLER)
    tasks = _text(MAIN_TASKS)
    for mode in ("0700", "0750", "2770", "0600", "0755", "0644", "0444"):
        assert mode in installer
        assert f'"{mode}"' in tasks


def test_libvirt_config_and_shared_provider_directories_are_fixed() -> None:
    for name in ("libvirtd-live.conf", "virtqemud-live.conf"):
        config = _text(SYSTEMD / name)
        assert 'unix_sock_group = "kdive-live-libvirt"' in config
        assert 'unix_sock_rw_perms = "0770"' in config
        assert 'unix_sock_dir = "/run/kdive/live-libvirt/libvirt"' in config
    tasks = _text(MAIN_TASKS)
    provisioning = tasks + _text(DEFAULTS)
    assert "XDG_RUNTIME_DIR: /run/kdive/live-libvirt" in tasks
    assert "/usr/sbin/libvirtd --daemon --config /etc/kdive/libvirtd-live.conf" in tasks
    assert "virtqemud" not in provisioning
    for path in (
        "/run/kdive/live-libvirt",
        "/var/lib/kdive/rootfs",
        "/var/lib/kdive/console",
        "/var/lib/kdive/pcap",
        "/var/lib/kdive/build",
        "/var/lib/kdive/install",
    ):
        assert path in provisioning


def test_socket_namespaces_are_traversable_but_not_worker_writable() -> None:
    installer = _text(INSTALLER)
    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)

    assert "install -d -o root -g root -m 0755 /run/kdive" in installer
    assert "_install_libvirt_runtime_directories" in installer
    assert "-m 0750" in installer
    assert "-m 2770" in installer

    parent = tasks[
        tasks.index("- name: Create the protected runtime parent") : tasks.index(
            "- name: Create the protected session-libvirt socket namespaces"
        )
    ]
    assert "path: /run/kdive" in parent
    assert "owner: root" in parent
    assert "group: root" in parent
    assert 'mode: "0755"' in parent

    namespaces = tasks[
        tasks.index("- name: Create the protected session-libvirt socket namespaces") : tasks.index(
            "- name: Create group-writable provider data directories"
        )
    ]
    assert 'mode: "0750"' in namespaces
    assert "live_vm_host_worker_libvirt_runtime + '/libvirt'" in namespaces

    provider_data = tasks[
        tasks.index("- name: Create group-writable provider data directories") : tasks.index(
            "- name: Install the fixed live-worker executables"
        )
    ]
    assert 'mode: "2770"' in provider_data
    assert "live_vm_host_worker_shared_directories" in provider_data

    for path in (
        "/run/kdive",
        "/run/kdive/live-libvirt",
        "/run/kdive/live-libvirt/libvirt",
    ):
        assert path in verify
    assert "Assert the runtime parent and socket namespace authority" in verify
    assert "Prove a worker cannot write protected socket namespaces" in verify
    assert "/usr/bin/test ! -w {{ item }}" in verify
    assert 'mode == "0755"' in verify
    assert 'mode == "0750"' in verify


def test_ansible_materializes_only_debian_libvirt_tuple() -> None:
    defaults = _text(DEFAULTS)
    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)
    assert "libvirtd-live.conf" in tasks
    assert "libvirt-sock" in defaults + tasks + verify
    assert "libvirtd.pid" in tasks
    assert "virtqemud" not in defaults + tasks + verify
    assert "pgrep" in verify and "libvirtd" in verify
    assert "--pidfile /run/kdive/live-libvirt/libvirt/libvirtd.pid" in verify


def test_selected_uri_file_is_public_root_owned_and_verified() -> None:
    installer = _text(INSTALLER)
    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)
    path = "/etc/kdive/live-worker-libvirt.env"
    for text in (installer, tasks, verify):
        assert path in text
    assert "printf 'KDIVE_LIBVIRT_URI=%s\\n' \"$_libvirt_uri\"" in installer
    assert "install -o root -g root -m 0644" in installer
    assert "KDIVE_LIBVIRT_URI={{ live_vm_host_worker_libvirt_uri }}" in tasks
    assert 'mode: "0644"' in tasks
    assert "owner: root" in tasks and "group: root" in tasks
    assert 'mode == "0644"' in verify


def test_installer_validates_selected_daemon_pid_and_socket_authority() -> None:
    source = _text(INSTALLER)
    assert '--pid-file "$libvirt_pid_path"' in source
    assert 'kill -0 "$_libvirt_tuple_pid"' in source
    assert "[[ ! -S $socket_path || -L $socket_path ]]" in source
    assert "stat -c '%u:%g:%a' \"$socket_path\"" in source
    assert '"$operator_uid:$group_gid:770"' in source


def _reconcile_libvirt_tuple(
    socket_path: Path, pid_path: Path, daemon: str
) -> subprocess.CompletedProcess[str]:
    command = r"""
source "$1"
_reconcile_libvirt_tuple "$2" "$3" "$4" "$5" "$6"
printf '%s\n' "$_libvirt_tuple_action"
"""
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            command,
            "bash",
            str(INSTALLER),
            str(socket_path),
            str(pid_path),
            daemon,
            str(os.getuid()),
            str(os.getgid()),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _stale_unix_socket(path: Path) -> None:
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
    path.chmod(0o770)


def test_installer_clears_only_proven_stale_selected_tuple(tmp_path: Path) -> None:
    socket_path = tmp_path / "libvirt-sock"
    pid_path = tmp_path / "libvirtd.pid"
    _stale_unix_socket(socket_path)
    pid_path.write_text("999999999\n", encoding="utf-8")

    result = _reconcile_libvirt_tuple(socket_path, pid_path, "libvirtd")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "start"
    assert not socket_path.exists()
    assert not pid_path.exists()
    source = _text(INSTALLER)
    assert "if [[ $_libvirt_tuple_action == start ]]" in source
    assert 'runuser -u "$operator"' in source


def test_installer_adopts_complete_matching_live_tuple(tmp_path: Path) -> None:
    socket_path = tmp_path / "libvirt-sock"
    pid_path = tmp_path / "libvirtd.pid"
    daemon = Path(f"/proc/{os.getpid()}/comm").read_text(encoding="utf-8").strip()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(socket_path))
        listener.listen()
        socket_path.chmod(0o770)
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

        result = _reconcile_libvirt_tuple(socket_path, pid_path, daemon)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "adopt"
    assert socket_path.exists()
    assert pid_path.exists()


def test_installer_leaves_contradictory_live_tuple_untouched(tmp_path: Path) -> None:
    socket_path = tmp_path / "libvirt-sock"
    pid_path = tmp_path / "libvirtd.pid"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(socket_path))
        listener.listen()
        socket_path.chmod(0o770)
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

        result = _reconcile_libvirt_tuple(socket_path, pid_path, "libvirtd")

    assert result.returncode != 0
    assert "contradictory selected libvirt tuple" in result.stderr
    assert "left untouched" in result.stderr
    assert socket_path.exists()
    assert pid_path.exists()


def test_installer_leaves_wrong_authority_residue_untouched(tmp_path: Path) -> None:
    socket_path = tmp_path / "libvirt-sock"
    pid_path = tmp_path / "libvirtd.pid"
    _stale_unix_socket(socket_path)
    socket_path.chmod(0o777)
    pid_path.write_text("999999999\n", encoding="utf-8")

    result = _reconcile_libvirt_tuple(socket_path, pid_path, "libvirtd")

    assert result.returncode != 0
    assert "wrong authority" in result.stderr
    assert "left untouched" in result.stderr
    assert socket_path.exists()
    assert pid_path.exists()


def test_installer_does_not_partially_remove_changed_stale_tuple(tmp_path: Path) -> None:
    socket_path = tmp_path / "libvirt-sock"
    pid_path = tmp_path / "libvirtd.pid"
    _stale_unix_socket(socket_path)
    pid_path.write_text("999999999\n", encoding="utf-8")
    replace_socket = """
import os
import socket
import sys

os.unlink(sys.argv[1])
with socket.socket(socket.AF_UNIX) as replacement:
    replacement.bind(sys.argv[1])
os.chmod(sys.argv[1], 0o770)
"""
    command = r"""
source "$1"
_inspect_libvirt_pid "$3" "$5" "$2"
_inspect_libvirt_socket "$2" "$5" "$6" "$3"
/usr/bin/python3 -c "$4" "$2"
_remove_stale_libvirt_tuple "$2" "$3"
"""

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            command,
            "bash",
            str(INSTALLER),
            str(socket_path),
            str(pid_path),
            replace_socket,
            str(os.getuid()),
            str(os.getgid()),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "changed during inspection" in result.stderr
    assert "left untouched" in result.stderr
    assert pid_path.exists()
    assert socket_path.exists()


def test_ansible_reconciles_complete_tuple_before_start() -> None:
    tasks = _text(MAIN_TASKS)
    start = tasks[tasks.index("- name: Start the operator-owned dedicated session libvirtd") :]
    assert "creates:" not in start
    for evidence in (
        "Inspect the selected libvirtd pid file",
        "Inspect the selected libvirt socket residue",
        "Probe the selected libvirt socket listener",
        "Inspect the selected libvirtd pid process",
        "Fail closed on contradictory selected libvirt evidence",
        "Remove proven stale selected libvirt residues",
    ):
        assert evidence in tasks
    assert tasks.index("Fail closed on contradictory selected libvirt evidence") < tasks.index(
        "Remove proven stale selected libvirt residues"
    )
    assert tasks.index("Remove proven stale selected libvirt residues") < tasks.index(
        "Start the operator-owned dedicated session libvirtd"
    )


def test_installer_has_no_compatibility_socket_alias() -> None:
    source = _text(INSTALLER)
    assert not re.search(r"\bln\b[^\n]*(?:libvirt|virtqemud)-sock", source)


def test_lifecycle_config_example_contains_no_database_authority() -> None:
    config = _text(SYSTEMD / "live-worker-lifecycle.conf.example")
    assert "KDIVE_LIVE_WORKER_OPERATOR_UID=1000" in config
    expected_state_root = (
        "KDIVE_LIVE_WORKER_STATE_ROOT="  # pragma: allowlist secret
        "/var/lib/kdive/live-workers"
    )
    assert expected_state_root in config
    assert "DATABASE" not in config
    assert "witness" not in config.lower()


def test_verification_covers_accounts_socket_virsh_and_slot_isolation() -> None:
    verify = _text(VERIFY_TASKS)
    for evidence in (
        "live_vm_host_worker_accounts",
        "sudo",
        "docker",
        "live_vm_host_worker_control_group",
        "/run/kdive/live-worker-lifecycle.sock",
        "ansible.builtin.systemd_service:",
        "systemd-analyze verify",
        "virsh -c {{ live_vm_host_worker_libvirt_uri }} list",
        "permission-probe",
        "sibling",
        "not replace",
    ):
        assert evidence in verify


def test_root_only_configuration_and_revision_are_verified() -> None:
    verify = _text(VERIFY_TASKS)
    for path in (
        "/etc/kdive/credentials/live-worker-witness.dsn",
        "/etc/kdive/live-worker-lifecycle.conf",
        "/opt/kdive-live-worker-lifecycle/revision",
    ):
        assert path in verify
    assert 'mode == "0600"' in verify
    assert 'mode == "0444"' in verify
