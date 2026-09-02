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
AUTHORITY_LIBVIRT_CONFIG = SYSTEMD / "libvirtd-external-boot-authority.conf"
AUTHORITY_ENV = SYSTEMD / "provider-authority.env.example"
AUTHORITY_SERVICE = SYSTEMD / "system" / "kdive-external-boot-authority.service"
AUTHORITY_TEARDOWN = ROOT / "deploy" / "ansible" / "playbooks" / "authority_host_teardown.yml"
AUTHORITY_PROOF = ROOT / "scripts" / "operations" / "prove-external-boot-authority-host.sh"
AUTHORITY_PREFLIGHT = ROLE / "tasks" / "authority_preflight.yml"
RUNNER_PLAY = ROOT / "deploy" / "ansible" / "playbooks" / "runner.yml"


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


def test_authority_endpoint_is_a_distinct_session() -> None:
    assert AUTHORITY_LIBVIRT_CONFIG.is_file(), "missing authority endpoint configuration"

    defaults = _yaml(DEFAULTS)
    assert defaults["live_vm_host_authority_enabled"] is False
    assert defaults["live_vm_host_authority_account"] == "kdive-provider-authority"
    assert defaults["live_vm_host_authority_client_group"] == ("kdive-provider-authority-client")
    assert defaults["live_vm_host_authority_runtime_root"] == ("/run/kdive/provider-authority")
    assert defaults["live_vm_host_authority_libvirt_uri"] == (
        "qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock"
    )
    assert defaults["live_vm_host_authority_denied_paths"] == [
        "/opt/kdive-provider-authority",
        "/etc/kdive/credentials/provider-authority",
        "/var/lib/kdive/provider-authority",
        "/var/lib/kdive/provider-authority/journal",
        "/run/kdive/provider-authority",
        "/run/kdive/provider-authority/request",
        "/run/kdive/provider-authority/libvirt",
    ]

    config = _text(AUTHORITY_LIBVIRT_CONFIG)
    assert 'unix_sock_group = "kdive-provider-authority"' in config
    assert 'unix_sock_rw_perms = "0700"' in config
    assert 'unix_sock_dir = "/run/kdive/provider-authority/libvirt"' in config
    assert "kdive-live-libvirt" not in config
    assert "/run/kdive/live-libvirt" not in config

    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)
    for evidence in (
        "Create the external-boot authority groups",
        "Create the external-boot authority account",
        "Inspect authority protected paths without following links",
        "Create authority protected paths",
        "Install the authority session-libvirt configuration",
        "Install the dormant authority session-libvirtd user unit",
        "Enable the dormant authority session-libvirtd user unit",
        "Start the dormant authority session libvirtd",
    ):
        assert evidence in tasks
    account = tasks[
        tasks.index("- name: Create the external-boot authority account") : tasks.index(
            "- name: Create the reconciler proof group"
        )
    ]
    assert "create_home: false" in account
    reconciler = tasks[
        tasks.index("- name: Create the reconciler denial-proof identity") : tasks.index(
            "- name: Enable linger for the external-boot authority account"
        )
    ]
    assert "home: /opt/kdive" in reconciler
    assert 'groups: ["{{ live_vm_host_authority_client_group }}"]' in tasks
    assert "loginctl enable-linger {{ live_vm_host_authority_account }}" in tasks
    runtime_environment = (
        "Environment=XDG_RUNTIME_DIR=/run/kdive/provider-authority"  # pragma: allowlist secret
    )
    assert runtime_environment in tasks
    assert (
        "ExecStart=/usr/sbin/libvirtd --daemon \\\n"
        "        --config /etc/kdive/libvirtd-external-boot-authority.conf \\\n"
        "        --pid-file /run/kdive/provider-authority/libvirt/libvirtd.pid" in tasks
    )
    for path in (
        "/opt/kdive-provider-authority",
        "/etc/kdive/credentials/provider-authority",
        "/var/lib/kdive/provider-authority/journal",
        "/run/kdive/provider-authority/request",
        "/run/kdive/provider-authority/libvirt",
    ):
        assert path in tasks
        assert path in verify
    assert "Assert the dormant authority endpoint is distinct and reachable" in verify
    assert "Verify the authority session-libvirtd user unit syntax" in verify
    assert "Prove fixed workers and the reconciler cannot traverse authority paths" in verify
    assert "(live_vm_host_worker_accounts + ['kdive'])" in verify
    assert "cannot access the authority mutation socket" in verify
    assert "cannot read the authority provider config" in verify
    assert "cannot access authority provider objects" in verify
    assert "virsh -c {{ live_vm_host_authority_libvirt_uri }} list" in verify


def test_existing_worker_provider_contract_is_preserved() -> None:
    defaults = _yaml(DEFAULTS)
    assert defaults["live_vm_host_worker_accounts"] == [
        f"kdive-worker-{slot}" for slot in range(1, 9)
    ]
    assert defaults["live_vm_host_worker_control_group"] == "kdive-live-control"
    assert defaults["live_vm_host_worker_libvirt_group"] == "kdive-live-libvirt"
    assert defaults["live_vm_host_worker_libvirt_runtime"] == "/run/kdive/live-libvirt"
    assert defaults["live_vm_host_worker_libvirt_uri"] == (
        "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"
    )
    assert _text(SYSTEMD / "libvirtd-live.conf") == (
        "# Dedicated session daemon shared by the operator and fixed KDIVE worker accounts.\n"
        'unix_sock_group = "kdive-live-libvirt"\n'
        'unix_sock_rw_perms = "0770"\n'
        'unix_sock_dir = "/run/kdive/live-libvirt/libvirt"\n'
    )
    assert _text(SYSTEMD / "system" / "kdive-live-worker@.service") == (
        "[Unit]\n"
        "Description=KDIVE retained live worker slot %i\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "User=kdive-worker-%i\n"
        "SupplementaryGroups=kdive-live-libvirt\n"
        "EnvironmentFile=/var/lib/kdive/live-workers/slots/%i/worker.env\n"
        "LoadCredential=worker-incarnation:"
        "/var/lib/kdive/live-workers/slots/%i/worker-incarnation.credential\n"
        "ExecStart=/usr/local/libexec/kdive-live-worker-gate %i\n"
        "Restart=no\n"
        "KillMode=control-group\n"
        "ExitType=cgroup\n"
        "RemainAfterExit=yes\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )

    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)
    assert 'groups: ["{{ live_vm_host_worker_libvirt_group }}", kvm]' in tasks
    assert "Start the operator-owned dedicated session libvirtd" in tasks
    assert "Verify existing worker provider path remains usable after authority endpoint" in verify
    assert "Verify every worker can use the KVM device" in verify
    assert "live_vm_host_authority_client_group" in verify
    assert "or live_vm_host_authority_client_group in" in verify


def test_ansible_installs_authority_in_clean_host_order() -> None:
    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)
    defaults = _yaml(DEFAULTS)

    assert AUTHORITY_SERVICE.is_file()
    assert AUTHORITY_ENV.is_file()
    assert AUTHORITY_TEARDOWN.is_file()
    assert AUTHORITY_PROOF.is_file()
    assert AUTHORITY_PREFLIGHT.is_file()
    assert defaults["live_vm_host_authority_runtime_install"] == "/opt/kdive-provider-authority"
    assert defaults["live_vm_host_authority_credentials_dir"] == (
        "/etc/kdive/credentials/provider-authority"
    )
    assert defaults["live_vm_host_authority_database_login"] == "kdive_authority_host"
    authority_packages = defaults["live_vm_host_authority_packages"]
    assert isinstance(authority_packages, list)
    assert "python3-psycopg" in authority_packages
    assert '- {path: /etc/kdive/credentials, mode: "0711"}' in tasks
    assert 'mode: "2750"' in tasks[tasks.index("Create authority protected children") :]

    ordered = (
        "Install external-boot authority prerequisites",
        "Create the external-boot authority groups",
        "Create the external-boot authority account",
        "Create authority protected paths",
        "Install KDIVE into the authority venv",
        "Install external-boot authority credentials",
        "Install the external-boot authority environment",
        "Install the external-boot authority service unit",
        "Start the dormant authority session libvirtd",
        "Start the external-boot authority service",
        "Run the one-shot authority readiness probe",
        "Create the transient authority proof identity",
        "Prove mutual TLS server client and worker authentication",
        "Retire the transient authority proof worker incarnation",
        "Remove the transient authority proof identity",
    )
    positions = [tasks.index(f"- name: {name}") for name in ordered]
    assert positions == sorted(positions)

    for evidence in (
        "Assert the authority service is ready",
        "Assert the authority database LOGIN is least privilege",
        "Prove fixed workers and the reconciler cannot traverse authority paths",
        "Verify existing worker provider path remains usable after authority endpoint",
        "Prove authority service restart restores readiness",
        "Prove authority readiness retracts on credential and ACL drift",
        "Prove journal restoration gates authority readiness",
    ):
        assert evidence in verify
    assert "NRestarts" in verify

    preflight = _text(AUTHORITY_PREFLIGHT)
    assert "Validate external-boot authority inputs before host mutation" in preflight
    for source in (
        "live_vm_host_authority_database_dsn_source",
        "live_vm_host_authority_server_key_source",
        "live_vm_host_authority_server_certificate_source",
        "live_vm_host_authority_server_ca_source",
        "live_vm_host_authority_worker_client_ca_source",
        "live_vm_host_authority_health_client_certificate_source",
        "live_vm_host_authority_health_client_key_source",
    ):
        assert source in preflight
    assert tasks.index("authority_preflight.yml") < tasks.index(
        "Install external-boot authority prerequisites"
    )
    runner_play = _text(RUNNER_PLAY)
    assert runner_play.index("authority_preflight.yml") < runner_play.index("roles:")

    proof = _text(AUTHORITY_PROOF)
    assert proof.startswith("#!/bin/bash\nset -euo pipefail\n")
    assert "git bundle create" in proof
    assert "live_vm_repo_url" in proof
    assert "live_vm_repo_version" in proof
    assert "git rev-parse HEAD" in proof
    assert "authority_host_teardown.yml" in proof
    assert "teardown pass 1" in proof
    assert "teardown pass 2" in proof
    assert "reboot: validating boot-persistent authority services" in proof
    assert "upgrade: installing exact revision" in proof
    assert "proof: exercising authority failure boundaries" in proof
    assert "converged: rerunning the exact revision" in proof
    assert "authority_inputs_digest" in proof
    assert "converged: unrelated changed task groups" in proof
    assert "changed=0" not in proof
    assert proof.index("upgrade: installing exact revision") < proof.index(
        "proof: exercising authority failure boundaries"
    )
    assert "--set=password=" not in proof
    assert "ub26-big.dev.pdx.drc.nz" not in proof
    assert "Clone the exact Git bundle for the venv" in tasks
    assert "live_vm_repo_url.endswith('.bundle')" in tasks
    assert "asyncio.IncompleteReadError" in tasks

    teardown = _text(AUTHORITY_TEARDOWN)
    assert "/usr/bin/python3" in teardown
    assert "from psycopg import connect, sql" in teardown
    assert "PGDATABASE:" not in teardown
    for evidence in (
        "Require the authority database administrative DSN",
        "Stop and disable the external-boot authority service",
        "Stop and disable the dormant authority endpoint",
        "Revoke the authority database LOGIN",
        "Assert the authority database LOGIN is revoked",
        "Assert authority services and processes are inactive",
        "Remove transient proof identity and material",
        "Assert authority units and endpoint artifacts are absent",
        "Assert retained authority evidence remains",
        "Verify the fixed-worker provider path remains usable",
    ):
        assert evidence in teardown
    assert "failed_when: false" not in teardown
    assert '"{{ authority_install }}"' in teardown

    assert "from psycopg import connect" in tasks
    assert "PGDATABASE:" not in tasks


def test_authority_teardown_reports_login_revocation_only_on_transition() -> None:
    document: object = yaml.safe_load(_text(AUTHORITY_TEARDOWN))
    assert isinstance(document, list)
    play = cast(dict[str, object], document[0])
    tasks = cast(list[object], play["tasks"])
    revoke = cast(
        dict[str, object],
        next(
            task
            for task in tasks
            if isinstance(task, dict) and task.get("name") == "Revoke the authority database LOGIN"
        ),
    )

    assert revoke["register"] == "authority_database_login_revocation"
    assert revoke["changed_when"] == (
        'authority_database_login_revocation.stdout == "login-revoked"'
    )
    assert revoke["no_log"] is True
    command = cast(dict[str, object], revoke["ansible.builtin.command"])
    argv = cast(list[object], command["argv"])
    script = argv[-1]
    assert isinstance(script, str)
    assert "SELECT rolcanlogin FROM pg_roles WHERE rolname=%s" in script
    assert 'print("login-revoked")' in script


def test_authority_services_restart_on_deployed_input_changes() -> None:
    tasks = _text(MAIN_TASKS)
    for registration in (
        "live_vm_host_authority_install",
        "live_vm_host_authority_credentials",
        "live_vm_host_authority_environment_result",
        "live_vm_host_authority_service_unit",
        "live_vm_host_authority_libvirt_config",
        "live_vm_host_authority_user_unit",
    ):
        assert f"register: {registration}" in tasks
    assert "Restart the dormant authority session libvirtd after deployed changes" in tasks
    assert "Restart the external-boot authority service after deployed changes" in tasks
    assert tasks.index(
        "Restart the external-boot authority service after deployed changes"
    ) < tasks.index("Run the one-shot authority readiness probe")


def test_authority_runtime_is_recreated_at_boot_with_exact_acl() -> None:
    tasks = _text(MAIN_TASKS)
    assert "Install the external-boot authority tmpfiles policy" in tasks
    assert "Apply the external-boot authority tmpfiles policy" in tasks
    assert "d /run/kdive/provider-authority 0710" in tasks
    assert "d /run/kdive/provider-authority/request 2750" in tasks
    assert "d /run/kdive/provider-authority/libvirt 0700" in tasks
    assert "ConditionPathIsDirectory=/run/kdive/provider-authority/libvirt" in tasks


def test_fixed_worker_user_unit_renders_the_configured_libvirt_group() -> None:
    tasks = _text(MAIN_TASKS)
    assert "ExecStart=/usr/bin/sg {{ live_vm_host_worker_libvirt_group }} -c" in tasks
    assert "ExecStart=/usr/bin/sg kdive-live-libvirt -c" not in tasks


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
    prepare = "_prepare_attested_runtime_root /opt/kdive-live-worker-lifecycle root root"
    assert source.index(prepare) < source.index(create)
    assert source.index(create) < source.index(install)
    assert "-m 0600" in source
    assert "/etc/kdive/credentials/live-worker-witness.dsn" in source
    assert "/opt/kdive-live-worker-lifecycle/revision" in source
    assert (
        "_link_system_guestfs_binding /opt/kdive-live-worker-lifecycle/.venv/bin/python" in source
    )
    ownership = "chown -R root:root /opt/kdive-live-worker-lifecycle"
    harden = "_harden_runtime_tree /opt/kdive-live-worker-lifecycle"
    assert source.index(ownership) < source.index(harden)
    assert "getent group kvm >/dev/null" in source
    assert '--groups "$libvirt_group,kvm"' in source
    assert 'usermod -G "$libvirt_group,kvm" "$worker"' in source


def test_installer_hardens_runtime_install_parent(tmp_path: Path) -> None:
    parent = tmp_path / "opt"
    parent.mkdir()
    parent.chmod(0o777)
    runtime_root = parent / "kdive-live-worker-lifecycle"
    command = r"""
source "$1"
_prepare_attested_runtime_root "$2" "$(id -u)" "$(id -g)"
"""

    result = subprocess.run(
        ["/bin/bash", "-c", command, "bash", str(INSTALLER), str(runtime_root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o755


def test_installer_clears_trusted_runtime_before_python_can_use_it(tmp_path: Path) -> None:
    parent = tmp_path / "opt"
    runtime_root = parent / "kdive-live-worker-lifecycle"
    planted = runtime_root / ".venv/lib/python3.14/sitecustomize.py"
    planted.parent.mkdir(parents=True)
    planted.write_text("raise RuntimeError('SENSITIVE_PREPLANT_SENTINEL')\n", encoding="utf-8")
    parent.chmod(0o755)
    runtime_root.chmod(0o755)
    command = r"""
source "$1"
_prepare_attested_runtime_root "$2" "$(id -u)" "$(id -g)"
"""

    result = subprocess.run(
        ["/bin/bash", "-c", command, "bash", str(INSTALLER), str(runtime_root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert list(runtime_root.iterdir()) == []
    assert "SENSITIVE_PREPLANT_SENTINEL" not in result.stdout
    assert "SENSITIVE_PREPLANT_SENTINEL" not in result.stderr


def test_installer_rejects_symlink_runtime_install_parent_without_path_output(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    parent = tmp_path / "SENSITIVE_PARENT_SENTINEL"
    parent.symlink_to(external, target_is_directory=True)
    runtime_root = parent / "runtime"
    command = r"""
source "$1"
_prepare_attested_runtime_root "$2" "$(id -u)" "$(id -g)"
"""

    result = subprocess.run(
        ["/bin/bash", "-c", command, "bash", str(INSTALLER), str(runtime_root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == (
        "lifecycle-runtime-error component=lifecycle_runtime_parent reason=not_real_directory\n"
    )
    assert "SENSITIVE_PARENT_SENTINEL" not in result.stderr


def test_installer_rejects_symlink_runtime_install_root(tmp_path: Path) -> None:
    parent = tmp_path / "opt"
    parent.mkdir()
    parent.chmod(0o755)
    external = tmp_path / "external"
    external.mkdir()
    external.chmod(0o777)
    runtime_root = parent / "SENSITIVE_ROOT_SENTINEL"
    runtime_root.symlink_to(external, target_is_directory=True)
    target_before = external.stat()
    command = r"""
source "$1"
_prepare_attested_runtime_root "$2" "$(id -u)" "$(id -g)"
"""

    result = subprocess.run(
        ["/bin/bash", "-c", command, "bash", str(INSTALLER), str(runtime_root)],
        check=False,
        capture_output=True,
        text=True,
    )
    target_after = external.stat()

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == (
        "lifecycle-runtime-error component=lifecycle_runtime_root reason=not_real_directory\n"
    )
    assert "SENSITIVE_ROOT_SENTINEL" not in result.stderr
    assert runtime_root.is_symlink()
    assert (
        target_after.st_uid,
        target_after.st_gid,
        stat.S_IMODE(target_after.st_mode),
    ) == (
        target_before.st_uid,
        target_before.st_gid,
        stat.S_IMODE(target_before.st_mode),
    )


def test_installer_hardens_runtime_tree_without_following_symlinks(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    nested = runtime_root / "nested"
    nested.mkdir(parents=True)
    installed = nested / "installed.py"
    installed.write_text("installed", encoding="utf-8")
    external = tmp_path / "external"
    external.write_text("external", encoding="utf-8")
    (nested / "external-link").symlink_to(external)
    for path in (runtime_root, nested, installed, external):
        path.chmod(0o777)

    command = r"""
source "$1"
_harden_runtime_tree "$2"
"""
    result = subprocess.run(
        ["/bin/bash", "-c", command, "bash", str(INSTALLER), str(runtime_root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o755
    assert stat.S_IMODE(nested.stat().st_mode) == 0o755
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    assert stat.S_IMODE(external.stat().st_mode) == 0o777


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
        "live-worker-libvirt.env",
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
    for mode in ("0700", "0711", "0750", "2770", "0600", "0755", "0644", "0640", "0444"):
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
    assert "XDG_RUNTIME_DIR=/run/kdive/live-libvirt" in tasks
    assert "/usr/sbin/libvirtd --daemon --config /etc/kdive/libvirtd-live.conf" in tasks
    assert "virtqemud" not in provisioning
    for path in (
        "/run/kdive/live-libvirt",
        "/var/lib/kdive/rootfs",
        "/var/lib/kdive/console",
        "/var/lib/kdive/pcap",
        "/var/lib/kdive/build",
        "/var/lib/kdive/install",
        "/var/lib/kdive/fixtures/local-libvirt",
    ):
        assert path in provisioning


def test_session_libvirtd_is_boot_persistent_via_user_unit() -> None:
    """The dedicated session daemon survives reboots (#2032): linger + an enabled user unit."""
    tasks = _text(MAIN_TASKS)
    defaults = _yaml(DEFAULTS)
    packages = defaults["live_vm_host_packages"]
    assert isinstance(packages, list)
    assert "login" in packages
    assert "kdive-libvirtd-live.service" in tasks
    assert (
        "ExecStart=/usr/bin/sg {{ live_vm_host_worker_libvirt_group }} -c \\\n"
        "        '/usr/sbin/libvirtd --daemon --config /etc/kdive/libvirtd-live.conf "
        "--pid-file /run/kdive/live-libvirt/libvirt/libvirtd.pid'" in tasks
    )
    # The user manager is reached through the runner's XDG_RUNTIME_DIR (no login session needed).
    enable = tasks.index("- name: Enable the boot-persistent session libvirtd user unit")
    start = tasks.index("- name: Start the operator-owned dedicated session libvirtd")
    assert "scope: user" in tasks[enable:start]
    # The unit's destination directory must exist before the copy: systemd does not
    # pre-create ~/.config/systemd/user and a clean host has no user units yet (#2044).
    ensure_dir = tasks.index("- name: Ensure the runner's systemd user unit directory exists")
    install = tasks.index("- name: Install the boot-persistent session libvirtd user unit")
    assert ensure_dir < install
    ensure_block = tasks[ensure_dir:install]
    assert "state: directory" in ensure_block
    assert 'mode: "0700"' in ensure_block
    assert "{{ ansible_facts.getent_passwd[github_runner_user][4] }}/.config/systemd/user" in (
        ensure_block
    )
    # Linger must be provisioned before the unit: it is what keeps the runner's user manager
    # alive from boot with no login session.
    assert tasks.index("loginctl enable-linger") < install
    assert enable < start, "the unit enables unconditionally; only the start is stale-gated"


def test_ansible_provisions_and_verifies_worker_accessible_fixture_catalog() -> None:
    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)
    assert "Create the fixed worker fixture catalog" in tasks
    assert "{{ role_path }}/../../../../fixtures/local-libvirt/{{ item }}" in tasks
    start = tasks.index("Install exact fixed worker fixture files")
    end = tasks.index("Install the fixed live-worker executables", start)
    assert "remote_src: true" not in tasks[start:end]
    assert "live_vm_host_worker_fixture_catalog" in tasks
    assert "Verify workers can access installed Python and provider paths" in verify
    assert "/opt/kdive-live-worker-lifecycle/.venv/bin/python" in verify
    assert "import guestfs, pathlib, kdive" in verify
    assert "/var/lib/kdive/build" in verify
    assert "Verify every worker can read every host kernel" in verify
    assert "Verify every worker can use the KVM device" in verify


def test_worker_access_verification_uses_the_shell_builtin_test() -> None:
    verify = _text(VERIFY_TASKS)
    start = verify.index("- name: Verify workers can access installed Python and provider paths")
    end = verify.index("- name: Verify every worker can read every host kernel", start)
    access_check = verify[start:end]

    assert "/bin/sh -c 'test -x" in access_check
    assert "/usr/bin/test" not in access_check
    assert "test -w /var/lib/kdive/build" in access_check


def test_socket_namespaces_are_traversable_but_not_worker_writable() -> None:
    installer = _text(INSTALLER)
    tasks = _text(MAIN_TASKS)
    verify = _text(VERIFY_TASKS)

    assert "_lock_libvirt_runtime /run/kdive /run/kdive/live-libvirt" in installer
    assert 'chmod 0755 "$runtime_parent"' in installer
    assert 'chmod 0750 "$runtime_root"' in installer
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
    assert "/run/kdive/live-worker-lifecycle.sock" in verify
    assert 'mode == "0660"' in verify


def test_installer_rejects_libvirt_child_symlink_without_touching_target(tmp_path: Path) -> None:
    runtime_parent = tmp_path / "kdive"
    runtime_root = runtime_parent / "live-libvirt"
    runtime_root.mkdir(parents=True)
    external_target = tmp_path / "external"
    external_target.mkdir(mode=0o711)
    target_before = external_target.stat()
    (runtime_root / "libvirt").symlink_to(external_target, target_is_directory=True)
    command = r"""
source "$1"
_lock_libvirt_runtime "$2" "$3" "$4" "$5" "$4"
"""

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            command,
            "bash",
            str(INSTALLER),
            str(runtime_parent),
            str(runtime_root),
            str(os.getuid()),
            str(os.getgid()),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    target_after = external_target.stat()
    assert result.returncode != 0
    assert "libvirt runtime child must be a real directory" in result.stderr
    assert (runtime_root / "libvirt").is_symlink()
    assert runtime_root.stat().st_uid == os.getuid()
    assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o750
    assert (target_after.st_uid, target_after.st_gid, stat.S_IMODE(target_after.st_mode)) == (
        target_before.st_uid,
        target_before.st_gid,
        stat.S_IMODE(target_before.st_mode),
    )


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
    assert "if [[ $libvirt_tuple_action == start ]]" in source
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
# Make the replacement observably different even on filesystems that immediately reuse the
# unlinked socket inode. The cleanup identity includes authority as well as device and inode.
os.chmod(sys.argv[1], 0o750)
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


def test_installer_stops_when_first_stale_unlink_fails(tmp_path: Path) -> None:
    socket_path = tmp_path / "libvirt-sock"
    pid_path = tmp_path / "libvirtd.pid"
    _stale_unix_socket(socket_path)
    pid_path.write_text("999999999\n", encoding="utf-8")
    command = r"""
source "$1"
unlink() { return 1; }
if ! _reconcile_libvirt_tuple "$2" "$3" libvirtd "$4" "$5"; then
  printf '%s\n' "$_libvirt_tuple_action"
  exit 1
fi
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
            str(os.getuid()),
            str(os.getgid()),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "could not remove stale pid residue" in result.stderr
    assert result.stdout.strip() == ""
    assert pid_path.exists()
    assert socket_path.exists()


def test_installer_rechecks_listener_before_stale_unlink(tmp_path: Path) -> None:
    socket_path = tmp_path / "libvirt-sock"
    pid_path = tmp_path / "libvirtd.pid"
    pid_path.write_text("999999999\n", encoding="utf-8")
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(socket_path))
        listener.listen()
        socket_path.chmod(0o770)
        command = r"""
source "$1"
_libvirt_tuple_pid=999999999
_libvirt_tuple_pid_identity="$(stat -c '%d:%i:%u:%a' "$3")"
_libvirt_tuple_socket_identity="$(stat -c '%d:%i:%u:%g:%a' "$2")"
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
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert "socket gained a listener during inspection" in result.stderr
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


def test_ansible_locks_and_identity_binds_tuple_recovery() -> None:
    tasks = _text(MAIN_TASKS)
    for evidence in (
        "Inspect the runtime root without following links",
        "Lock the session-libvirt runtime root",
        "Inspect the libvirt child without following links",
        "Lock the selected libvirt tuple hierarchy",
        "Refresh selected libvirt residue identity before cleanup",
        "Reinspect the selected libvirtd process before cleanup",
        "Reprobe the selected libvirt socket listener before cleanup",
        "Assert selected libvirt evidence did not change under lock",
        "Restore the session-libvirt runtime hierarchy",
    ):
        assert evidence in tasks
    assert tasks.count("follow: false") >= 6
    assert "always:" in tasks
    assert tasks.index("Lock the selected libvirt tuple hierarchy") < tasks.index(
        "Inspect the selected libvirtd pid file"
    )
    assert tasks.index("Refresh selected libvirt residue identity before cleanup") < tasks.index(
        "Remove proven stale selected libvirt residues"
    )
    assert tasks.index("Assert selected libvirt evidence did not change under lock") < tasks.index(
        "Remove proven stale selected libvirt residues"
    )
    assert tasks.index("Reinspect the selected libvirtd process before cleanup") < tasks.index(
        "Remove proven stale selected libvirt residues"
    )
    assert tasks.index("Reprobe the selected libvirt socket listener before cleanup") < tasks.index(
        "Remove proven stale selected libvirt residues"
    )
    assert tasks.index("Restore the session-libvirt runtime hierarchy") < tasks.index(
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
    ):
        assert evidence in verify


def test_root_only_configuration_and_revision_are_verified() -> None:
    verify = _text(VERIFY_TASKS)
    for path in (
        "/etc/kdive/credentials/live-worker-witness.dsn",
        "/etc/kdive/live-worker-lifecycle.conf",
        "/etc/kdive/live-worker-libvirt.env",
        "/opt/kdive-live-worker-lifecycle/revision",
    ):
        assert path in verify
    assert 'mode == "0600"' in verify
    assert 'mode == "0644"' in verify
    assert 'mode == "0444"' in verify
