"""Provisioning contract for opt-in local external-boot authority mutation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "deploy" / "ansible" / "roles" / "provider_authority_host"
UNIT = ROLE / "templates" / "authority.service.j2"
SYSTEM_PROVISIONING_VALIDATOR = (
    ROOT / "deploy" / "ansible" / "scripts" / "validate_authority_system_provisioning.py"
)


def _yaml(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected mapping in {path}")
    return cast(dict[str, object], value)


def _canonical_manifest(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _run_system_provisioning_validator(
    value: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SYSTEM_PROVISIONING_VALIDATOR)],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=False,
    )


def test_local_mutation_is_disabled_by_default() -> None:
    defaults = _yaml(ROLE / "defaults" / "main.yml")
    assert defaults["provider_authority_host_local_mutation_enabled"] is False
    assert defaults["provider_authority_host_recovery_root"] == (
        "/var/lib/kdive/provider-authority/recovery"
    )
    assert defaults["provider_authority_host_external_boot_capacity_bytes"] is None
    assert defaults["provider_authority_host_s3_endpoint_url"] == ""
    assert defaults["provider_authority_host_s3_bucket"] == ""
    assert defaults["provider_authority_host_s3_region"] == "us-east-1"
    assert defaults["provider_authority_host_s3_credentials_source"] == ""
    assert defaults["provider_authority_host_remote_module_enabled"] is False
    assert defaults["provider_authority_host_remote_module_architectures"] == []
    assert defaults["provider_authority_host_remote_libvirt_storage_pool"] == "default"
    assert defaults["provider_authority_host_fault_proof_enabled"] is False


def test_enabled_environment_contains_complete_mutation_configuration() -> None:
    template = Environment(undefined=StrictUndefined).from_string(
        (ROLE / "templates" / "environment.j2").read_text(encoding="utf-8")
    )
    rendered = template.render(
        provider_authority_host_instance="authority-test",
        provider_authority_host_uid=991,
        provider_authority_host_gid=992,
        provider_authority_host_client_gid=993,
        provider_authority_host_network_address="",
        provider_authority_host_network_port=None,
        provider_authority_host_denied_identities=["operator"],
        provider_authority_host_local_mutation_enabled=True,
        provider_authority_host_recovery_root="/var/lib/kdive/provider-authority/recovery",
        provider_authority_host_rootfs_root="/var/lib/kdive/provider-authority/rootfs",
        provider_authority_host_console_root="/var/lib/kdive/provider-authority/console",
        provider_authority_host_external_boot_capacity_bytes=34359738368,
        provider_authority_host_s3_endpoint_url="https://objects.example.invalid",
        provider_authority_host_s3_bucket="artifacts",
        provider_authority_host_s3_region="us-east-1",
        provider_authority_host_remote_module_enabled=True,
        provider_authority_host_remote_module_architectures=["x86_64"],
        provider_authority_host_remote_libvirt_storage_pool="authority-systems",
        provider_authority_host_fault_proof_enabled=False,
    )
    assert (
        "KDIVE_LIBVIRT_RECOVERY_ROOT=" + "/var/lib/kdive/provider-authority/recovery\n" in rendered
    )
    assert "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES=" + "34359738368\n" in rendered
    assert "KDIVE_S3_ENDPOINT_URL=https://objects.example.invalid\n" in rendered
    assert "KDIVE_S3_BUCKET=artifacts\n" in rendered
    assert "KDIVE_S3_REGION=us-east-1\n" in rendered
    assert "KDIVE_EXTERNAL_BOOT_AUTHORITY_REMOTE_MODULE_ENABLED=true\n" in rendered
    assert "KDIVE_EXTERNAL_BOOT_AUTHORITY_REMOTE_MODULE_ARCHITECTURES=x86_64\n" in rendered
    # pragma: allowlist nextline secret -- fixed storage-pool name, not a credential
    assert "KDIVE_REMOTE_LIBVIRT_STORAGE_POOL=authority-systems\n" in rendered


def test_preflight_rejects_partial_or_invalid_local_mutation_before_sources() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "preflight.yml").read_text(encoding="utf-8"))
    validation = next(
        task for task in tasks if "closed authority deployment inputs" in task["name"]
    )
    script = validation["ansible.builtin.command"]["argv"][-1]
    assert "local_mutation" in script
    assert "capacity" in script
    assert "endpoint" in script
    assert "bucket" in script
    assert "region" in script
    assert "s3_credentials" in script
    assert "fault_proof" in script
    assert "urlsplit" in script
    assert validation["no_log"] is True
    system_validation = next(
        task
        for task in tasks
        if task["name"]
        == "Verify canonical authority System manifest and base digests on the controller"
    )
    assert system_validation["delegate_to"] == "localhost"
    assert system_validation["become"] is False
    assert system_validation["no_log"] is True
    assert (
        "validate_authority_system_provisioning.py"
        in system_validation["ansible.builtin.command"]["argv"][-1]
    )
    source_check = next(
        task
        for task in tasks
        if task["name"] == "Inspect authority credential sources on the controller"
    )
    assert tasks.index(validation) < tasks.index(source_check)
    assert "provider_authority_host_s3_credentials_source" in str(source_check["loop"])
    profile = next(
        task
        for task in tasks
        if task["name"] == "Validate the protected S3 shared-credentials profile"
    )
    assert profile["delegate_to"] == "localhost"
    assert profile["become"] is False
    assert profile["no_log"] is True
    assert "aws_access_key_id" in profile["ansible.builtin.command"]["argv"][-2]
    assert "aws_secret_access_key" in profile["ansible.builtin.command"]["argv"][-2]


@pytest.mark.parametrize(
    ("override", "accepted"),
    [
        ({}, True),
        (
            {
                "local_mutation": False,
                "capacity": None,
                "endpoint": "",
                "bucket": "",
                "s3_credentials": "",
            },
            True,
        ),
        ({"capacity": None}, False),
        ({"capacity": 0}, False),
        ({"endpoint": "objects.example.invalid"}, False),
        ({"endpoint": "https://user:" + "not-a-real-secret@objects.example.invalid"}, False),
        ({"bucket": ""}, False),
        ({"s3_credentials": "relative/credentials"}, False),
        ({"remote_module": True}, False),
        ({"remote_module": True, "architectures": []}, False),
        ({"remote_module": True, "architectures": ["aarch64"]}, False),
        ({"remote_module": True, "pool": "bad/name"}, False),
        ({"fault_proof": True}, True),
        ({"fault_proof": "true"}, False),
        (
            {
                "local_mutation": False,
                "capacity": None,
                "endpoint": "",
                "bucket": "",
                "s3_credentials": "",
                "remote_module": True,
                "architectures": ["x86_64"],
                "pool": "authority-systems",
            },
            False,
        ),
        (
            {
                "remote_module": True,
                "local_mutation": True,
                "architectures": ["x86_64"],
                "pool": "authority-systems",
            },
            True,
        ),
        (
            {
                "system_manifest": "/protected/manifest.json",
                "system_local_bases": [{"source": "/protected/base.qcow2", "digest": "a" * 64}],
            },
            True,
        ),
        ({"system_manifest": "/protected/manifest.json"}, False),
    ],
)
def test_local_mutation_preflight_is_executable_and_fails_closed(
    override: dict[str, object], accepted: bool
) -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "preflight.yml").read_text(encoding="utf-8"))
    validation = next(
        task for task in tasks if "closed authority deployment inputs" in task["name"]
    )
    script = validation["ansible.builtin.command"]["argv"][-1]
    values: dict[str, object] = {
        "enabled": True,
        "address": "",
        "port": None,
        "source": "",
        "gdb_range": "47000:47099",
        "management_port": 22,
        "family": "Debian",
        "instance": "authority-test",
        "denied": [os.environ["USER"]],
        "paths": ["/opt/kdive", "/var/lib/kdive/provider-authority/recovery"],
        "local_mutation": True,
        "capacity": 34359738368,
        "endpoint": "https://objects.example.invalid",
        "bucket": "artifacts",
        "region": "us-east-1",
        "s3_credentials": "/protected/s3-credentials",
        "remote_module": False,
        "architectures": [],
        "pool": "default",
        "system_manifest": "",
        "system_local_bases": [],
        "system_remote_bases": [],
        "fault_proof": False,
    }
    values.update(override)
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(values),
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted
    if not accepted:
        assert result.stderr.strip() == "invalid or incomplete provider authority deployment inputs"


def test_system_provisioning_validator_requires_exact_local_manifest_mapping(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"local-base")
    base.chmod(0o600)
    digest = sha256(base.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(
        _canonical_manifest(
            {
                "schema": "authority-system-manifest-v1",
                "provider_kind": "local-libvirt",
                "resource_name": "local-resource",
                "authority_instance": "authority-test",
                "bases": [
                    {
                        "root_identity": f"sha256:{digest}",
                        "architecture": "x86_64",
                        "source_kind": "local",
                        "source_name": None,
                    }
                ],
            }
        )
    )
    manifest.chmod(0o600)
    value: dict[str, object] = {
        "manifest": str(manifest),
        "authority_instance": "authority-test",
        "expected_provider_kind": "local-libvirt",
        "local_bases": [{"source": str(base), "digest": digest}],
        "remote_bases": [],
    }

    assert _run_system_provisioning_validator(value).returncode == 0

    base.write_bytes(b"different-base")
    assert _run_system_provisioning_validator(value).returncode != 0

    base.write_bytes(b"local-base")
    value["local_bases"] = []
    assert _run_system_provisioning_validator(value).returncode != 0

    extra = tmp_path / "extra.qcow2"
    extra.write_bytes(b"extra-base")
    extra.chmod(0o600)
    value["local_bases"] = [
        {"source": str(base), "digest": digest},
        {"source": str(extra), "digest": sha256(extra.read_bytes()).hexdigest()},
    ]
    assert _run_system_provisioning_validator(value).returncode != 0


def test_system_provisioning_validator_rejects_wrong_kind_and_accepts_remote_mapping(
    tmp_path: Path,
) -> None:
    base = tmp_path / "remote-base.qcow2"
    base.write_bytes(b"remote-base")
    base.chmod(0o600)
    digest = sha256(base.read_bytes()).hexdigest()
    manifest = tmp_path / "remote-manifest.json"
    manifest.write_bytes(
        _canonical_manifest(
            {
                "schema": "authority-system-manifest-v1",
                "provider_kind": "remote-libvirt",
                "resource_name": "remote-resource",
                "authority_instance": "authority-test",
                "entries": [
                    {
                        "root_identity": f"sha256:{digest}",
                        "architecture": "x86_64",
                        "base_volume": "remote-base.qcow2",
                        "network": "provider-net",
                        "machine": "pc-q35-9.2",
                        "gdb_addr": "127.0.0.1",
                        "gdb_port_min": 31000,
                        "gdb_port_max": 31002,
                        "ssh_addr": "127.0.0.1",
                        "ssh_port_min": 32000,
                        "ssh_port_max": 32002,
                    }
                ],
            }
        )
    )
    manifest.chmod(0o600)
    value: dict[str, object] = {
        "manifest": str(manifest),
        "authority_instance": "authority-test",
        "expected_provider_kind": "local-libvirt",
        "local_bases": [],
        "remote_bases": [{"source": str(base), "name": "remote-base.qcow2"}],
    }

    assert _run_system_provisioning_validator(value).returncode != 0

    value["expected_provider_kind"] = "remote-libvirt"
    assert _run_system_provisioning_validator(value).returncode == 0


def test_local_mutation_installs_owner_only_root_tools_and_s3_credentials() -> None:
    defaults = _yaml(ROLE / "defaults" / "main.yml")
    local_packages = cast(dict[str, list[str]], defaults["provider_authority_host_local_packages"])
    assert "libguestfs-tools" in local_packages["Debian"]
    assert "python3-guestfs" in local_packages["Debian"]
    assert "libguestfs-tools-c" in local_packages["RedHat"]
    assert "python3-libguestfs" in local_packages["RedHat"]
    qemu = cast(dict[str, dict[str, str]], defaults["provider_authority_host_qemu_packages"])
    assert qemu["Debian"]["x86_64"] == "qemu-system-x86"
    assert qemu["RedHat"]["ppc64le"] == "qemu-kvm"

    tasks = (ROLE / "tasks" / "install.yml").read_text(encoding="utf-8")
    assert "['kdive-provider-authority-client', 'kvm']" in tasks
    assert "provider_authority_host_local_mutation_enabled | bool" in tasks
    assert "provider_authority_host_recovery_root" in tasks
    assert 'mode: "0700"' in tasks
    assert "s3-credentials" in tasks
    assert 'mode: "0400"' in tasks
    assert "Symlink the libguestfs binding into the authority venv" in tasks


def test_authority_unit_projects_s3_credentials_and_only_needed_devices() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    assert "LoadCredential=s3-credentials:" in unit
    assert "Environment=AWS_SHARED_CREDENTIALS_FILE=%d/s3-credentials" in unit
    private_devices = (
        "PrivateDevices={{ 'no' if provider_authority_host_local_mutation_enabled else 'yes' }}"
    )
    assert private_devices in unit
    assert "DeviceAllow=/dev/kvm rw" in unit
    assert "ReadWritePaths={{ provider_authority_host_recovery_root }}" in unit
    assert "ProtectSystem=strict" in unit
    assert "NoNewPrivileges=yes" in unit
    template = Environment(undefined=StrictUndefined).from_string(unit)
    identity_only = template.render(
        provider_authority_host_local_mutation_enabled=False,
        provider_authority_host_remote_module_enabled=False,
        provider_authority_host_fault_proof_enabled=False,
    )
    assert "PrivateDevices=yes" in identity_only
    assert "DeviceAllow=" not in identity_only
    assert "s3-credentials" not in identity_only
    assert "provider-authority/recovery" not in identity_only
    mutation = template.render(
        provider_authority_host_local_mutation_enabled=True,
        provider_authority_host_recovery_root="/var/lib/kdive/provider-authority/recovery",
        provider_authority_host_rootfs_root="/var/lib/kdive/provider-authority/rootfs",
        provider_authority_host_console_root="/var/lib/kdive/provider-authority/console",
        provider_authority_host_remote_module_enabled=True,
        provider_authority_host_fault_proof_enabled=True,
    )
    assert "PrivateDevices=no" in mutation
    assert "DevicePolicy=closed" in mutation
    assert "DeviceAllow=/dev/kvm rw" in mutation
    assert "ReadWritePaths=/var/lib/kdive/provider-authority/recovery" in mutation
    assert (
        "ReadWritePaths=/var/lib/kdive/provider-authority/remote-module-preparations/evidence"
        in mutation
    )
    assert (
        "ReadWritePaths=/var/lib/kdive/provider-authority/remote-module-preparations/work"
        in mutation
    )
    assert "remote-module-preparations" not in identity_only
    assert "ReadWritePaths=/run/kdive/provider-authority/proof-control" in mutation


def test_fault_proof_projects_only_the_fixed_private_socket_runtime() -> None:
    environment = Environment(undefined=StrictUndefined).from_string(
        (ROLE / "templates" / "environment.j2").read_text(encoding="utf-8")
    )
    values = {
        "provider_authority_host_instance": "authority-test",
        "provider_authority_host_uid": 991,
        "provider_authority_host_gid": 992,
        "provider_authority_host_client_gid": 993,
        "provider_authority_host_network_address": "",
        "provider_authority_host_network_port": None,
        "provider_authority_host_denied_identities": ["operator"],
        "provider_authority_host_local_mutation_enabled": False,
        "provider_authority_host_remote_module_enabled": False,
        "provider_authority_host_fault_proof_enabled": False,
    }
    dormant = environment.render(**values)
    enabled = environment.render(**(values | {"provider_authority_host_fault_proof_enabled": True}))
    setting = (
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_PROOF_SOCKET="
        "/run/kdive/provider-authority/proof-control/control.sock"
    )
    assert setting not in dormant
    assert setting in enabled

    install = (ROLE / "tasks" / "install.yml").read_text(encoding="utf-8")
    assert (
        "d /run/kdive/provider-authority/proof-control 0700 "
        "kdive-provider-authority kdive-provider-authority -"
    ) in install
    assert "{% if provider_authority_host_fault_proof_enabled | bool %}" in install
    assert install.count("/run/kdive/provider-authority/proof-control") == 3
    assert "Refuse a substituted fault-proof runtime" in install
    assert "Create the owner-only fault-proof runtime" in install


def test_authority_system_installation_is_opt_in_and_keeps_bases_read_only() -> None:
    defaults = _yaml(ROLE / "defaults" / "main.yml")
    assert defaults["provider_authority_host_system_manifest_source"] == ""
    assert defaults["provider_authority_host_system_local_bases"] == []
    assert defaults["provider_authority_host_system_remote_bases"] == []

    tasks = yaml.safe_load((ROLE / "tasks" / "install.yml").read_text(encoding="utf-8"))
    names = [task["name"] for task in tasks]
    for name in (
        "Inspect authority System provisioning paths without following links",
        "Create private authority System provisioning paths",
        "Install the canonical authority System manifest",
        "Install owner-only local authority System bases",
        "Install owner-only remote authority System bases",
    ):
        assert name in names
    assert names.index("Install the canonical authority System manifest") < names.index(
        "Start the configured authority after firewall and protected inputs converge"
    )

    local_bases = next(
        task for task in tasks if task["name"] == "Install owner-only local authority System bases"
    )
    remote_bases = next(
        task for task in tasks if task["name"] == "Install owner-only remote authority System bases"
    )
    for task in (local_bases, remote_bases):
        copy = task["ansible.builtin.copy"]
        assert copy["owner"] == "kdive-provider-authority"
        assert copy["group"] == "kdive-provider-authority"
        assert copy["mode"] == "0400"
        assert copy["follow"] is False

    template = Environment(undefined=StrictUndefined).from_string(UNIT.read_text(encoding="utf-8"))
    disabled = template.render(
        provider_authority_host_local_mutation_enabled=False,
        provider_authority_host_remote_module_enabled=False,
        provider_authority_host_fault_proof_enabled=False,
        provider_authority_host_system_manifest_source="",
        provider_authority_host_system_local_bases=[],
        provider_authority_host_system_remote_bases=[],
    )
    assert "system-provisioning" not in disabled
    enabled = template.render(
        provider_authority_host_local_mutation_enabled=True,
        provider_authority_host_recovery_root="/var/lib/kdive/provider-authority/recovery",
        provider_authority_host_rootfs_root="/var/lib/kdive/provider-authority/rootfs",
        provider_authority_host_console_root="/var/lib/kdive/provider-authority/console",
        provider_authority_host_remote_module_enabled=False,
        provider_authority_host_fault_proof_enabled=False,
        provider_authority_host_system_manifest_source="/controller/manifest.json",
        provider_authority_host_system_local_bases=[
            {"digest": "a" * 64, "source": "/controller/base"}
        ],
        provider_authority_host_system_remote_bases=[],
    )
    assert (
        "ReadWritePaths=/var/lib/kdive/provider-authority/system-provisioning/system-operations"
        in enabled
    )
    assert (
        "ReadWritePaths=/var/lib/kdive/provider-authority/system-provisioning/local/state"
        in enabled
    )
    assert (
        "ReadWritePaths=/var/lib/kdive/provider-authority/system-provisioning/local/rootfs/systems"
        in enabled
    )
    assert (
        "ReadWritePaths=/var/lib/kdive/provider-authority/system-provisioning/local/rootfs/baselines"
        in enabled
    )
    assert "system-provisioning/local/rootfs/bases" not in enabled

    remote = template.render(
        provider_authority_host_local_mutation_enabled=True,
        provider_authority_host_recovery_root="/var/lib/kdive/provider-authority/recovery",
        provider_authority_host_rootfs_root="/var/lib/kdive/provider-authority/rootfs",
        provider_authority_host_console_root="/var/lib/kdive/provider-authority/console",
        provider_authority_host_remote_module_enabled=True,
        provider_authority_host_fault_proof_enabled=False,
        provider_authority_host_system_manifest_source="/controller/manifest.json",
        provider_authority_host_system_local_bases=[],
        provider_authority_host_system_remote_bases=[
            {"name": "remote-base.qcow2", "source": "/controller/base"}
        ],
    )
    assert (
        "ReadWritePaths=/var/lib/kdive/provider-authority/system-provisioning/system-operations"
        in remote
    )
    assert "ReadWritePaths=/var/lib/kdive/provider-authority/system-provisioning/remote" in remote
    assert "system-provisioning/local/rootfs" not in remote


def test_remote_module_private_pool_uses_the_authority_session_daemon() -> None:
    tasks = (ROLE / "tasks" / "libvirt.yml").read_text(encoding="utf-8")
    assert "/var/lib/kdive/provider-authority/remote-libvirt-pool" in tasks
    assert "provider_authority_host_remote_libvirt_storage_pool" in tasks
    assert "qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock" in tasks
    assert "provider_authority_host_remote_module_enabled | default(false) | bool" in tasks


def test_runbook_describes_a_complete_local_mutation_vars_file() -> None:
    runbook = (ROOT / "docs" / "operating" / "runbooks" / "self-hosted-kvm-runner.md").read_text(
        encoding="utf-8"
    )
    for name in (
        "provider_authority_host_local_mutation_enabled",
        "provider_authority_host_external_boot_capacity_bytes",
        "provider_authority_host_s3_endpoint_url",
        "provider_authority_host_s3_bucket",
        "provider_authority_host_s3_credentials_source",
    ):
        assert name in runbook


def test_local_fixed_workers_receive_protected_authority_client_files() -> None:
    defaults = _yaml(ROLE.parent / "live_vm_host" / "defaults" / "main.yml") | _yaml(
        ROLE.parent / "local_worker_host" / "defaults" / "main.yml"
    )
    assert defaults["live_vm_host_worker_authority_enabled"] is False
    assert defaults["live_vm_host_worker_authority_request_socket"] == (
        "/run/kdive/provider-authority/request/authority.sock"
    )
    assert defaults["live_vm_host_worker_authority_server_ca_ref"] == (
        "external-boot-authority/server-ca"
    )
    tasks = (ROLE.parent / "live_vm_host" / "tasks" / "main.yml").read_text(encoding="utf-8")
    tasks += (ROLE.parent / "local_worker_host" / "tasks" / "worker_accounts.yml").read_text(
        encoding="utf-8"
    )
    assert "Create the fixed-worker authority client group before account membership" in tasks
    assert "Install protected fixed-worker authority TLS files" in tasks
    assert 'mode: "0440"' in tasks
    assert "live_vm_host_worker_authority_client_certificate_source" in tasks
    assert "live_vm_host_worker_authority_client_key_source" in tasks
    preflight = (ROLE.parent / "live_vm_host" / "tasks" / "authority_preflight.yml").read_text(
        encoding="utf-8"
    )
    assert "Require the complete fixed-worker authority client contract" in preflight
    assert "Require protected regular fixed-worker authority client sources" in preflight
    assert "delegate_to: localhost" in preflight
    assert "no_log: true" in preflight
