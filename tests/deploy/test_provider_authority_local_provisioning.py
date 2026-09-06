"""Provisioning contract for opt-in local external-boot authority mutation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "deploy" / "ansible" / "roles" / "provider_authority_host"
UNIT = ROLE / "templates" / "authority.service.j2"


def _yaml(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected mapping in {path}")
    return cast(dict[str, object], value)


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
    assert "urlsplit" in script
    assert validation["no_log"] is True
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
    defaults = _yaml(ROLE.parent / "live_vm_host" / "defaults" / "main.yml")
    assert defaults["live_vm_host_worker_authority_enabled"] is False
    assert defaults["live_vm_host_worker_authority_request_socket"] == (
        "/run/kdive/provider-authority/request/authority.sock"
    )
    assert defaults["live_vm_host_worker_authority_server_ca_ref"] == (
        "external-boot-authority/server-ca"
    )
    tasks = (ROLE.parent / "live_vm_host" / "tasks" / "main.yml").read_text(encoding="utf-8")
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
