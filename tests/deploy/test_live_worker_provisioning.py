"""Structural proofs for the fixed live-worker host provisioning contract."""

from __future__ import annotations

import re
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
        "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock"
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


def test_installer_and_ansible_install_the_same_fixed_files() -> None:
    installer = _text(INSTALLER)
    tasks = _text(MAIN_TASKS)
    names = (
        "kdive-live-worker-gate",
        "kdive-live-worker-lifecycle",
        "kdive-live-worker@.service",
        "kdive-live-worker-lifecycle.socket",
        "kdive-live-worker-lifecycle@.service",
        "virtqemud-live.conf",
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
    config = _text(SYSTEMD / "virtqemud-live.conf")
    assert 'unix_sock_group = "kdive-live-libvirt"' in config
    assert 'unix_sock_rw_perms = "0770"' in config
    tasks = _text(MAIN_TASKS)
    provisioning = tasks + _text(DEFAULTS)
    assert "XDG_RUNTIME_DIR: /run/kdive/live-libvirt" in tasks
    assert "virtqemud --daemon --config /etc/kdive/virtqemud-live.conf" in tasks
    for path in (
        "/run/kdive/live-libvirt",
        "/var/lib/kdive/rootfs",
        "/var/lib/kdive/console",
        "/var/lib/kdive/pcap",
        "/var/lib/kdive/build",
        "/var/lib/kdive/install",
    ):
        assert path in provisioning


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
