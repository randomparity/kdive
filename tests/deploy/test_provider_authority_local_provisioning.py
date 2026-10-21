"""Provisioning contract for opt-in local external-boot authority mutation."""

from __future__ import annotations

import json
import os
import re
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
        ({"appliance_root": "/private/other"}, True),
        (
            {
                "remote_module": True,
                "architectures": ["x86_64"],
                "appliance_root": "/private/other",
            },
            False,
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
        "system_manifest": "",
        "system_local_bases": [],
        "system_remote_bases": [],
        "fault_proof": False,
        "appliance_root": "/var/lib/kdive/remote-module-appliance/v1",
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


def test_selected_appliances_are_checked_as_authority_before_start_and_handlers() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/install.yml").read_text())
    probe = _named(
        tasks, "Verify selected installed appliances as the authority before service start"
    )
    assert probe["become"] is True
    assert probe["become_user"] == "kdive-provider-authority"
    assert probe["changed_when"] is False
    assert probe["when"] == "provider_authority_host_remote_module_enabled | bool"
    assert probe["timeout"] == 60
    assert probe["loop"] == "{{ provider_authority_host_remote_module_architectures }}"
    assert (
        tasks.index(probe)
        < tasks.index(_named(tasks, "Apply deployed-input restarts before proving readiness"))
        < tasks.index(
            _named(
                tasks, "Start the configured authority after firewall and protected inputs converge"
            )
        )
    )


def test_authority_preflight_renders_the_appliance_default_before_other_role_defaults() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/preflight.yml").read_text())
    validation = next(t for t in tasks if "closed authority deployment inputs" in t["name"])
    environment = Environment(undefined=StrictUndefined)
    environment.filters["to_json"] = json.dumps
    template = environment.from_string(validation["ansible.builtin.command"]["stdin"])
    values = _yaml(ROLE / "defaults/main.yml") | {"ansible_facts": {"os_family": "Debian"}}
    assert json.loads(template.render(values))["appliance_root"] == (
        "/var/lib/kdive/remote-module-appliance/v1"
    )
    values["remote_libvirt_module_appliance_install_dir"] = "/private/other"
    assert json.loads(template.render(values))["appliance_root"] == "/private/other"


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


@pytest.mark.parametrize(("accel", "accepted"), [("kvm", True), ("tcg", True), ("other", False)])
def test_system_provisioning_validator_accepts_supported_accelerators(
    tmp_path: Path, accel: str, accepted: bool
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
                "accel": accel,
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

    assert (_run_system_provisioning_validator(value).returncode == 0) is accepted


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
    assert "ProtectKernelModules=yes" in identity_only
    assert "RestrictSUIDSGID=yes" in identity_only
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
    # libguestfs builds its appliance from /lib/modules, which ProtectKernelModules hides.
    assert "ProtectKernelModules=no" in mutation
    # supermin unpacks a base image that carries setuid/setgid entries.
    assert "RestrictSUIDSGID=no" in mutation
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


def test_authority_system_bases_carry_a_static_virt_content_label() -> None:
    defaults = _yaml(ROLE / "defaults" / "main.yml")
    packages = cast(dict[str, list[str]], defaults["provider_authority_host_packages"])
    assert "policycoreutils-python-utils" in packages["RedHat"]
    tasks = yaml.safe_load((ROLE / "tasks" / "install.yml").read_text(encoding="utf-8"))
    names = [task["name"] for task in tasks]
    by_name = {task["name"]: task for task in tasks}
    probe = by_name["Read the SELinux mode for the authority System bases"]
    rules = by_name["Install the authority System base virt_content_t fcontext rules"]
    relabel = by_name["Relabel the authority System bases"]
    destinations = (
        "{{ provider_authority_host_system_base_destinations.results | default([])\n"
        "   | map(attribute='item') | list }}"
    )
    selinux_enabled = (
        "provider_authority_host_selinux_mode.stdout | default('') in ['Enforcing', 'Permissive']"
    )

    assert probe["ansible.builtin.command"]["argv"] == ["getenforce"]
    assert probe["changed_when"] is False
    assert probe["failed_when"] is False
    assert rules["community.general.sefcontext"] == {
        "target": "{{ item | regex_escape }}",
        "setype": "virt_content_t",
        "state": "present",
    }
    assert relabel["ansible.builtin.command"]["argv"] == ["restorecon", "-v", "{{ item }}"]
    assert "Relabeled" in relabel["changed_when"]
    assert relabel["notify"] == "Restart provider authority after deployed changes"
    for task in (rules, relabel):
        assert task["loop"] == destinations
        assert selinux_enabled in task["when"]
    assert (
        names.index("Inspect authority System base destinations without following links")
        < names.index(probe["name"])
        < names.index(rules["name"])
        < names.index("Install owner-only local authority System bases")
        < names.index("Install owner-only remote authority System bases")
        < names.index(relabel["name"])
        < names.index("Start the configured authority after firewall and protected inputs converge")
    )


def test_remote_module_private_pool_uses_the_authority_session_daemon() -> None:
    tasks = (ROLE / "tasks" / "libvirt.yml").read_text(encoding="utf-8")
    assert "/var/lib/kdive/provider-authority/remote-libvirt-pool" in tasks
    assert "provider_authority_host_remote_libvirt_storage_pool" in tasks
    assert "qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock" in tasks
    assert "provider_authority_host_remote_module_enabled | default(false) | bool" in tasks


def _tasks(path: Path) -> list[dict[str, object]]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise TypeError(f"expected task list in {path}")
    return cast(list[dict[str, object]], value)


def _named(tasks: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(task for task in tasks if task.get("name") == name)


MEMLOCK_DROP_IN = (
    "/etc/systemd/system/user@{{ provider_authority_host_uid }}.service.d/kdive-memlock.conf"
)


def test_authority_session_libvirtd_gets_an_unlimited_memlock_ceiling() -> None:
    # libvirt always sets RLIMIT_MEMLOCK for a ppc64 guest, and an unprivileged session daemon
    # cannot raise it past its hard limit, so the role must raise both the user manager's and
    # the unit's limit before the daemon starts (#2767).
    tasks = _tasks(ROLE / "tasks" / "libvirt.yml")
    drop_in = _named(tasks, "Raise the authority user manager memlock ceiling for ppc64 guests")
    copy = cast(dict[str, str], drop_in["ansible.builtin.copy"])
    assert copy["dest"] == MEMLOCK_DROP_IN
    assert copy["content"] == "[Service]\nLimitMEMLOCK=infinity\n"
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0644")
    restart = _named(tasks, "Restart the authority user manager to apply its memlock ceiling")
    service = cast(dict[str, object], restart["ansible.builtin.systemd_service"])
    assert service == {
        "name": "user@{{ provider_authority_host_uid }}.service",
        "state": "restarted",
        "daemon_reload": True,
    }
    # An interrupted converge can leave the drop-in written but the manager unrestarted, so the
    # restart also fires while the running manager's own hard limit is not yet unlimited.
    limit = _named(tasks, "Read the authority user manager locked-memory limit")
    limit_argv = cast(dict[str, list[str]], limit["ansible.builtin.command"])["argv"]
    assert limit_argv[:2] == ["prlimit", "--memlock"]
    assert limit["environment"] == {"LC_ALL": "C"}
    assert restart["when"] == [
        f"{drop_in['register']} is changed"
        f" or {limit['register']}.stdout | default('') | trim != 'unlimited'"
    ]
    assert tasks.index(drop_in) < tasks.index(limit) < tasks.index(restart)
    unit_task = _named(tasks, "Install the dormant authority session-libvirtd user unit")
    unit = cast(dict[str, str], unit_task["ansible.builtin.copy"])["content"]
    assert "\nLimitMEMLOCK=infinity\n" in unit.split("[Service]", 1)[1].split("[Install]")[0]
    start = _named(tasks, "Start the dormant authority session libvirtd")
    assert tasks.index(drop_in) < tasks.index(restart) < tasks.index(unit_task)
    assert tasks.index(restart) < tasks.index(start)


def test_both_authority_call_sites_verify_the_running_memlock_limit() -> None:
    verify = _tasks(ROLE / "tasks" / "libvirt_verify.yml")
    limit = _named(verify, "Read the authority session libvirtd locked-memory limit")
    argv = cast(dict[str, list[str]], limit["ansible.builtin.command"])["argv"]
    assert argv[:2] == ["prlimit", "--memlock"]
    assert limit["environment"] == {"LC_ALL": "C"}
    check = _named(verify, "Require an unlimited authority session libvirtd memlock limit")
    that = cast(dict[str, object], check["ansible.builtin.assert"])["that"]
    assert f"{limit['register']}.stdout.split() == ['unlimited', 'unlimited']" in cast(
        list[str], that
    )
    installed = _tasks(ROLE / "tasks" / "install.yml")
    flush = _named(installed, "Apply deployed-input restarts before proving readiness")
    verified = next(
        task
        for task in installed
        if task.get("ansible.builtin.import_tasks") == "libvirt_verify.yml"
    )
    assert installed.index(flush) < installed.index(verified)
    runner = _tasks(ROLE.parent / "live_vm_host" / "tasks" / "main.yml")
    runner_flush = _named(runner, "Apply authority service restarts before readiness validation")
    runner_verify = next(
        task
        for task in runner
        if task.get("ansible.builtin.include_tasks")
        == "../../provider_authority_host/tasks/libvirt_verify.yml"
    )
    assert runner.index(runner_flush) < runner.index(runner_verify)
    assert runner_verify["when"] == "live_vm_host_authority_enabled | bool"
    assert cast(dict[str, str], runner_verify["vars"]) == {
        "provider_authority_host_account": "{{ live_vm_host_authority_account }}",
        "provider_authority_host_uid": "{{ live_vm_host_authority_uid }}",
    }


def test_retiring_the_authority_removes_its_memlock_drop_in() -> None:
    tasks = _tasks(ROLE / "tasks" / "disable.yml")
    removal = _named(tasks, "Remove the retired authority user manager memlock drop-in")
    path = cast(dict[str, str], removal["ansible.builtin.file"])
    assert path["state"] == "absent"
    assert path["path"].endswith(".service.d/kdive-memlock.conf")
    assert removal["notify"] == "Reload systemd after authority removal"
    flush = _named(tasks, "Apply authority removal before completing cleanup")
    assert tasks.index(removal) < tasks.index(flush)


def test_retiring_the_authority_restarts_a_user_manager_holding_the_retired_ceiling() -> None:
    # Removing the drop-in only changes the unit's configured limit; a lingering authority user
    # manager keeps its unlimited ceiling until it restarts (ADR-0708). The restart precedes
    # disabling linger, which may stop the manager instead.
    tasks = _tasks(ROLE / "tasks" / "disable.yml")
    known = "ansible_facts.getent_passwd['kdive-provider-authority'] | default(none) is not none"
    manager = "user@{{ ansible_facts.getent_passwd['kdive-provider-authority'][1] }}.service"
    pid = _named(tasks, "Read the retiring authority user manager main PID")
    configured = _named(tasks, "Read the retiring authority user manager configured memlock limit")
    running = _named(tasks, "Read the retiring authority user manager running memlock limit")
    restart = _named(
        tasks, "Restart the retiring authority user manager to drop its memlock ceiling"
    )
    # A re-run whose drop-in removal reports ok notifies no handler, and systemctl show reports
    # the loaded limit until a reload, so the reload before the reads is unconditional.
    reload = _named(tasks, "Reload systemd before reading the retiring authority user manager")
    assert reload["ansible.builtin.systemd_service"] == {"daemon_reload": True}
    assert "when" not in reload
    show = ["/usr/bin/systemctl", "show"]
    assert cast(dict[str, list[str]], pid["ansible.builtin.command"])["argv"] == [
        *show,
        "--property=MainPID",
        "--value",
        manager,
    ]
    assert cast(dict[str, list[str]], configured["ansible.builtin.command"])["argv"] == [
        *show,
        "--property=LimitMEMLOCK",
        "--value",
        manager,
    ]
    assert cast(dict[str, list[str]], running["ansible.builtin.command"])["argv"] == [
        "/usr/bin/prlimit",
        "--memlock",
        "--noheadings",
        "--output=HARD",
        f"--pid={{{{ {pid['register']}.stdout }}}}",
    ]
    assert running["environment"] == {"LC_ALL": "C"}
    assert restart["ansible.builtin.systemd_service"] == {"name": manager, "state": "restarted"}
    running_pid = f'{pid["register"]}.stdout not in ["", "0"]'
    assert pid["when"] == configured["when"] == known
    assert running["when"] == [known, running_pid]
    assert restart["when"] == [
        known,
        running_pid,
        f"{running['register']}.stdout | trim == 'unlimited'",
        f"{configured['register']}.stdout | trim != 'infinity'",
    ]
    order = [
        _named(tasks, "Remove the retired authority user manager memlock drop-in"),
        reload,
        pid,
        configured,
        running,
        restart,
        _named(tasks, "Retire authority user-service boot persistence"),
        _named(tasks, "Apply authority removal before completing cleanup"),
    ]
    assert [tasks.index(t) for t in order] == sorted(tasks.index(t) for t in order)


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
    live_tasks = ROLE.parent / "live_vm_host" / "tasks"
    assert "ansible.builtin.import_tasks: worker_authority_tls.yml" in (
        live_tasks / "main.yml"
    ).read_text(encoding="utf-8")
    tasks = (live_tasks / "worker_authority_tls.yml").read_text(encoding="utf-8")
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


@pytest.mark.parametrize("layout", ["dist-packages", "lib64/python3.14/site-packages"])
def test_authority_discovers_native_guestfs_and_verifies_installed_import(
    tmp_path: Path, layout: str
) -> None:
    tasks = _tasks(ROLE / "tasks" / "install.yml")
    locate = _named(tasks, "Locate the system libguestfs binding files")
    command = cast(dict[str, list[str]], locate["ansible.builtin.command"])["argv"]
    assert command[0] == "{{ provider_authority_host_python }}"
    native = tmp_path / layout
    native.mkdir(parents=True)
    for name in ("guestfs", "libguestfsmod"):
        (native / (name + ".py")).write_text("", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, *command[1:]],
        env={**os.environ, "PYTHONPATH": str(native)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        str(native / "guestfs.py"),
        str(native / "libguestfsmod.py"),
    ]
    site = _named(tasks, "Locate the authority venv site-packages")
    site_argv = cast(dict[str, list[str]], site["ansible.builtin.command"])["argv"]
    assert site_argv[0] == "/opt/kdive-provider-authority/.venv/bin/python"
    assert "sysconfig.get_path('platlib')" in site_argv[-1]
    link = _named(tasks, "Symlink the libguestfs binding into the authority venv")
    assert "provider_authority_host_venv_site.stdout" in str(link["ansible.builtin.file"])
    verify = _named(tasks, "Verify the authority venv imports the native libguestfs binding")
    verify_argv = cast(dict[str, list[str]], verify["ansible.builtin.command"])["argv"]
    assert verify_argv == [site_argv[0], "-c", "import guestfs, libguestfsmod"]
    assert tasks.index(locate) < tasks.index(site) < tasks.index(link) < tasks.index(verify)
    for task in (locate, site, link, verify):
        assert task["when"] == "provider_authority_host_local_mutation_enabled | bool"

    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(tmp_path / "venv")], check=True
    )
    python = tmp_path / "venv/bin/python"
    destination = subprocess.check_output([str(python), *site_argv[1:]], text=True).strip()
    environment = Environment(undefined=StrictUndefined)
    environment.filters["basename"] = lambda value: Path(value).name
    link_args = cast(dict[str, str], link["ansible.builtin.file"])
    for source in json.loads(result.stdout):
        target = environment.from_string(link_args["dest"]).render(
            provider_authority_host_venv_site={"stdout": destination}, item=source
        )
        Path(target).symlink_to(source)
    imported = subprocess.run([str(python), *verify_argv[1:]], capture_output=True, check=False)
    assert imported.returncode == 0, imported.stderr
    (native / "libguestfsmod.py").write_text("raise ImportError('unusable native binding')\n")
    broken = subprocess.run([str(python), *verify_argv[1:]], capture_output=True, check=False)
    assert broken.returncode != 0
    assert b"unusable native binding" in broken.stderr


LIVE_TASKS = ROLE.parent / "live_vm_host" / "tasks"
CARRIER = ROOT / "scripts" / "operations" / "prove-external-boot-authority-host.sh"
AUTHORITY_UNITS = (
    UNIT,
    ROLE.parent / "live_vm_host" / "templates" / "external-boot-authority.service.j2",
    ROOT / "deploy" / "systemd" / "system" / "kdive-external-boot-authority.service",
)


def _flattened(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    flat: list[dict[str, object]] = []
    for task in tasks:
        flat.append(task)
        for section in ("block", "rescue", "always"):
            flat.extend(_flattened(cast(list[dict[str, object]], task.get(section, []))))
    return flat


def _expiry_includes(path: Path) -> list[dict[str, object]]:
    return [
        task
        for task in _flattened(_tasks(path))
        if cast(dict[str, object], task.get("ansible.builtin.include_tasks", {})).get("file")
        == "tls_expiry.yml"
    ]


def _certificates(task: dict[str, object]) -> list[tuple[str, str]]:
    variables = cast(dict[str, list[dict[str, str]]], task["vars"])
    certificates = variables["live_vm_host_tls_expiry_certificates"]
    return [(item["name"], item["path"]) for item in certificates]


def test_tls_expiry_checks_cover_authority_and_worker_certificates() -> None:
    preflight = _expiry_includes(LIVE_TASKS / "authority_preflight.yml")
    assert [_certificates(task) for task in preflight] == [
        [
            ("server-ca", "{{ live_vm_host_worker_authority_server_ca_source }}"),
            ("client-certificate", "{{ live_vm_host_worker_authority_client_certificate_source }}"),
        ],
        [
            ("server-certificate", "{{ live_vm_host_authority_server_certificate_source }}"),
            ("server-ca", "{{ live_vm_host_authority_server_ca_source }}"),
            ("worker-client-ca", "{{ live_vm_host_authority_worker_client_ca_source }}"),
            (
                "health-client-certificate",
                "{{ live_vm_host_authority_health_client_certificate_source }}",
            ),
        ],
    ]
    for task in preflight:
        include = cast(dict[str, object], task["ansible.builtin.include_tasks"])
        assert include["apply"] == {"delegate_to": "localhost", "become": False}
    assert preflight[0]["when"] == "live_vm_host_worker_authority_enabled | bool"

    verify_tasks = _tasks(LIVE_TASKS / "verify.yml")
    verify = _expiry_includes(LIVE_TASKS / "verify.yml")
    installed = "{{ live_vm_host_authority_credentials_dir }}"
    worker = "{{ live_vm_host_worker_authority_secrets_root }}"
    assert [_certificates(task) for task in verify] == [
        [
            (name, f"{installed}/{name}")
            for name in (
                "server-certificate",
                "server-ca",
                "worker-client-ca",
                "health-client-certificate",
            )
        ],
        [(name, f"{worker}/{name}") for name in ("server-ca", "client-certificate")],
    ]
    assert [task["when"] for task in verify] == [
        "live_vm_host_authority_enabled | bool",
        "live_vm_host_worker_authority_enabled | bool",
    ]
    ready = verify_tasks.index(_named(verify_tasks, "Assert the authority service is ready"))
    for task in verify:
        assert "apply" not in cast(dict[str, object], task["ansible.builtin.include_tasks"])
        assert verify_tasks.index(task) < ready
    for task in preflight + verify:
        assert all("key" not in path for _, path in _certificates(task))

    margin = _yaml(ROLE.parent / "live_vm_host" / "defaults" / "main.yml")[
        "live_vm_host_authority_tls_expiry_margin_seconds"
    ]
    assert margin == 43200
    # The disposable carrier issues one-day certificates and provisions through this role.
    assert re.findall(r"--days (\S+)", CARRIER.read_text(encoding="utf-8")) == ["1"] * 4
    assert cast(int, margin) < 86400


def _directives(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if "=" in line and not line.startswith(("#", "{"))
    )


def _seconds(value: str) -> int:
    return int(value.removesuffix("s"))


@pytest.mark.parametrize("path", AUTHORITY_UNITS, ids=lambda path: path.name)
def test_authority_units_bound_their_restart_loop(path: Path) -> None:
    unit_text, service_text = path.read_text(encoding="utf-8").split("[Service]", 1)
    unit, service = _directives(unit_text), _directives(service_text)
    assert unit["StartLimitIntervalSec"] == "600"
    assert unit["StartLimitBurst"] == "5"
    assert "StartLimitIntervalSec" not in service
    assert "StartLimitBurst" not in service
    interval, burst = int(unit["StartLimitIntervalSec"]), int(unit["StartLimitBurst"])
    restart, timeout = _seconds(service["RestartSec"]), _seconds(service["TimeoutStartSec"])
    # Even starts that hang until their timeout must reach the limit inside one interval.
    assert burst * (timeout + restart) < interval


def test_drift_proof_resets_the_start_limit_before_restoring() -> None:
    proof = _named(
        _tasks(LIVE_TASKS / "verify.yml"),
        "Prove authority readiness retracts on credential and ACL drift",
    )
    always = cast(list[dict[str, object]], proof["always"])
    restore = always.index(_named(always, "Restore authority readiness after drift proofs"))
    reset = always[restore - 1]
    assert cast(dict[str, object], reset["ansible.builtin.command"])["argv"] == [
        "systemctl",
        "reset-failed",
        "kdive-external-boot-authority.service",
    ]
    assert reset["changed_when"] is False
