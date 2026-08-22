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
    assert 'groups: ["{{ live_vm_host_worker_libvirt_group }}", kvm]' in tasks
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
    assert "Symlink the libguestfs binding into the lifecycle worker venv" in tasks


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
    assert (
        "_link_system_guestfs_binding /opt/kdive-live-worker-lifecycle/.venv/bin/python" in source
    )
    assert "getent group kvm >/dev/null" in source
    assert '--groups "$libvirt_group,kvm"' in source
    assert 'usermod -G "$libvirt_group,kvm" "$worker"' in source


def test_installer_reports_socket_activation_failure_context() -> None:
    source = _text(INSTALLER)

    assert "could not enable the live-worker lifecycle socket" in source
    assert "systemctl status --no-pager --full kdive-live-worker-lifecycle.socket" in source


def test_installer_provisions_the_fixed_worker_fixture_catalog() -> None:
    source = _text(INSTALLER)
    assert "install_fixed_fixture_catalog" in source
    assert "/var/lib/kdive/fixtures/local-libvirt" in source
    assert "--no-dereference" in source
    assert "_fixture_files" in source
    assert '/var/lib/kdive/fixtures/local-libvirt 0 "$libvirt_group_gid"' in source


def _fixture_catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / "source" / "fixtures" / "local-libvirt"
    (catalog / "profiles").mkdir(parents=True)
    for relative in (
        "manifest.yaml",
        "rootfs_catalog.toml",
        "profiles/console-ready_ppc64le.yaml",
        "profiles/console-ready_x86_64.yaml",
    ):
        (catalog / relative).write_text(relative, encoding="utf-8")
    return catalog


def _install_fixture_catalog(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    command = r"""
source "$1"
install_fixed_fixture_catalog "$2" "$3" "$(id -u)" "$(id -g)"
"""
    return subprocess.run(
        ["/bin/bash", "-c", command, "bash", str(INSTALLER), str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_fixture_copy_is_exact_and_removes_stale_files(tmp_path: Path) -> None:
    source = _fixture_catalog(tmp_path)
    destination = tmp_path / "installed" / "local-libvirt"
    destination.mkdir(parents=True)
    (destination / "stale").write_text("stale", encoding="utf-8")
    result = _install_fixture_catalog(source, destination)
    assert result.returncode == 0, result.stderr
    assert sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*")) == [
        "manifest.yaml",
        "profiles",
        "profiles/console-ready_ppc64le.yaml",
        "profiles/console-ready_x86_64.yaml",
        "rootfs_catalog.toml",
    ]
    assert all(path.stat().st_mode & 0o777 == 0o640 for path in destination.rglob("*.yaml"))


def test_installer_fixture_copy_rejects_source_or_destination_links(tmp_path: Path) -> None:
    source = _fixture_catalog(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    source_link = tmp_path / "source-link"
    source_link.symlink_to(source, target_is_directory=True)
    source_result = _install_fixture_catalog(source_link, tmp_path / "installed" / "catalog")
    assert source_result.returncode != 0
    destination_link = tmp_path / "installed" / "catalog"
    destination_link.parent.mkdir(parents=True)
    destination_link.symlink_to(external, target_is_directory=True)
    destination_result = _install_fixture_catalog(source, destination_link)
    assert destination_result.returncode != 0
    assert not list(external.iterdir())


def test_installer_fixture_copy_rejects_a_symlinked_source_ancestor(tmp_path: Path) -> None:
    source = _fixture_catalog(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    linked_fixtures = tmp_path / "source" / "fixtures"
    linked_fixtures.rename(external / "fixtures")
    linked_fixtures.symlink_to(external / "fixtures", target_is_directory=True)
    destination = tmp_path / "installed" / "local-libvirt"
    result = _install_fixture_catalog(source, destination)
    assert result.returncode != 0
    assert not destination.exists()


def test_installer_fixture_copy_preserves_a_converged_catalog(tmp_path: Path) -> None:
    source = _fixture_catalog(tmp_path)
    destination = tmp_path / "installed" / "local-libvirt"
    first = _install_fixture_catalog(source, destination)
    assert first.returncode == 0, first.stderr
    before = (destination / "manifest.yaml").stat()
    second = _install_fixture_catalog(source, destination)
    assert second.returncode == 0, second.stderr
    after = (destination / "manifest.yaml").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_installer_is_an_executable_host_contract() -> None:
    assert INSTALLER.stat().st_mode & stat.S_IXUSR


def test_ansible_creates_shared_slots_parent_before_private_children() -> None:
    installer = _text(INSTALLER)
    tasks = _text(MAIN_TASKS)
    parent_start = tasks.index("- name: Create the shared slots parent")
    child_start = tasks.index("- name: Create root-owned fixed slot directories")
    assert parent_start < child_start
    parent = tasks[parent_start:child_start]
    assert 'path: "{{ live_vm_host_worker_state_root }}/slots"' in parent
    assert "owner: root" in parent
    assert "group: root" in parent
    assert 'mode: "0711"' in parent
    assert 'install -d -o root -g root -m 0711 "$state_root/slots"' in installer


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
    for mode in ("0700", "0711", "0750", "0600", "0755", "0644", "0640", "0444"):
        assert mode in installer
        assert f'"{mode}"' in tasks


def test_ansible_provisions_and_verifies_worker_accessible_fixture_catalog() -> None:
    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)
    assert "Create the fixed worker fixture catalog" in tasks
    assert "{{ role_path }}/../../../../fixtures/local-libvirt/{{ item }}" in tasks
    start = tasks.index("Install exact fixed worker fixture files")
    end = tasks.index("Install the fixed live-worker executables", start)
    assert "remote_src: true" not in tasks[start:end]
    assert "live_vm_host_worker_fixture_catalog" in tasks
    assert "Verify workers can access installed Python and fixture paths" in verify
    assert "/opt/kdive-live-worker-lifecycle/.venv/bin/python" in verify
    assert "import guestfs, pathlib, kdive" in verify
    assert "Verify every worker can read every host kernel" in verify
    assert "Verify every worker can use the KVM device" in verify


def test_worker_access_verification_uses_the_shell_builtin_test() -> None:
    verify = _text(VERIFY_TASKS)
    start = verify.index("- name: Verify workers can access installed Python and fixture paths")
    end = verify.index("- name: Verify every worker can read every host kernel", start)
    access_check = verify[start:end]

    assert "/bin/sh -c 'test -x" in access_check
    assert "/usr/bin/test" not in access_check


def test_protected_runtime_parent_is_converged_by_both_paths() -> None:
    installer = _text(INSTALLER)
    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)

    assert "install -d -o root -g root -m 0755 /run/kdive" in installer

    parent = tasks[
        tasks.index("- name: Create the protected runtime parent") : tasks.index(
            "- name: Inspect the fixed worker fixture parent without following links"
        )
    ]
    assert "path: /run/kdive" in parent
    assert "owner: root" in parent
    assert "group: root" in parent
    assert 'mode: "0755"' in parent

    assert "Assert the runtime parent authority" in verify
    assert 'mode == "0755"' in verify
    assert "Prove a worker cannot write the protected runtime parent" in verify
    assert "/run/kdive/live-worker-lifecycle.sock" in verify
    assert 'mode == "0660"' in verify


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


def test_verification_covers_accounts_socket_and_slot_isolation() -> None:
    verify = _text(VERIFY_TASKS)
    for evidence in (
        "live_vm_host_worker_accounts",
        "sudo",
        "docker",
        "live_vm_host_worker_control_group",
        "/run/kdive/live-worker-lifecycle.sock",
        "ansible.builtin.systemd_service:",
        "systemd-analyze verify",
        "permission-probe",
        "sibling",
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


def test_libvirt_daemon_authority_stays_with_the_follow_up_change() -> None:
    """#1937 owns the session-libvirt daemon confs, runtime hierarchy, and URI publication."""
    for text in (
        _text(INSTALLER),
        _text(DEFAULTS),
        _text(MAIN_TASKS),
        _text(VERIFY_TASKS),
    ):
        assert "virtqemud" not in text
        assert "libvirtd --daemon" not in text
        assert "live-worker-libvirt.env" not in text
        assert "KDIVE_LIBVIRT_URI" not in text
        assert "/run/kdive/live-libvirt" not in text
