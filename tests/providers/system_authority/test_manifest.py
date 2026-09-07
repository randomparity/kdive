from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.providers.ports.external_boot import RootSpecV1
from kdive.providers.system_authority.composition import (
    build_local_authority_system_provider,
    validate_authority_system_installation,
)
from kdive.providers.system_authority.manifest import (
    LocalAuthoritySystemManifestV1,
    RemoteAuthoritySystemManifestV1,
    load_authority_system_manifest,
)
from kdive.providers.system_authority.protocol import (
    AuthoritySystemCommitContextV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemProvisionSnapshot,
)

_DIGEST = "sha256:" + "a" * 64


def _local_document() -> dict[str, Any]:
    return {
        "schema": "authority-system-manifest-v1",
        "provider_kind": "local-libvirt",
        "resource_name": "local-a",
        "authority_instance": "authority-a",
        "bases": [
            {
                "root_identity": _DIGEST,
                "architecture": "x86_64",
                "source_kind": "local",
            }
        ],
    }


def _remote_document() -> dict[str, Any]:
    return {
        "schema": "authority-system-manifest-v1",
        "provider_kind": "remote-libvirt",
        "resource_name": "remote-a",
        "authority_instance": "authority-a",
        "entries": [
            {
                "root_identity": _DIGEST,
                "architecture": "x86_64",
                "base_volume": "base-a.qcow2",
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


def _write_manifest(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o400)


def test_loads_closed_local_manifest_from_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    _write_manifest(path, _local_document())

    result = load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())

    assert isinstance(result, LocalAuthoritySystemManifestV1)
    assert result.bases[0].filename == f"{'a' * 64}.qcow2"


def test_loads_and_validates_remote_provider_entry(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    _write_manifest(path, _remote_document())

    result = load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())

    assert isinstance(result, RemoteAuthoritySystemManifestV1)
    assert result.provider_entries()[0].base_volume == "base-a.qcow2"


@pytest.mark.parametrize("mode", (0o600, 0o440))
def test_rejects_manifest_with_non_owner_only_mode(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "manifest.json"
    _write_manifest(path, _local_document())
    path.chmod(mode)

    with pytest.raises(ValueError, match="unsafe"):
        load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_rejects_manifest_hardlink(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    alias = tmp_path / "alias.json"
    _write_manifest(path, _local_document())
    os.link(path, alias)

    with pytest.raises(ValueError, match="unsafe"):
        load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_rejects_manifest_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    path = tmp_path / "manifest.json"
    _write_manifest(source, _local_document())
    path.symlink_to(source)

    with pytest.raises(OSError):
        load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_rejects_unknown_manifest_field(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    document = {**_local_document(), "caller_path": "/tmp/base.qcow2"}
    _write_manifest(path, document)

    with pytest.raises(ValidationError):
        load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_rejects_noncanonical_manifest(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_local_document(), indent=2) + "\n")
    path.chmod(0o400)

    with pytest.raises(ValueError, match="not canonical"):
        load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_fifo_substitution_fails_without_waiting_for_a_writer(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    os.mkfifo(path, mode=0o400)
    probe = """
import os
import sys
from pathlib import Path
from kdive.providers.system_authority.manifest import load_authority_system_manifest
try:
    load_authority_system_manifest(Path(sys.argv[1]), owner_uid=os.getuid(), owner_gid=os.getgid())
except ValueError:
    raise SystemExit(0)
raise SystemExit(1)
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe, str(path)],
        check=False,
        timeout=2,
    )

    assert completed.returncode == 0


def test_rejects_duplicate_root_binding(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    document = _local_document()
    bases = document["bases"]
    assert isinstance(bases, list)
    bases.append(dict(bases[0]))
    _write_manifest(path, document)

    with pytest.raises(ValidationError, match="duplicate root binding"):
        load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_rejects_catalog_selector_bound_to_multiple_roots(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    document = _local_document()
    document["bases"] = [
        {
            "root_identity": _DIGEST,
            "architecture": "x86_64",
            "source_kind": "catalog",
            "source_name": "base-a",
        },
        {
            "root_identity": "sha256:" + "b" * 64,
            "architecture": "x86_64",
            "source_kind": "catalog",
            "source_name": "base-a",
        },
    ]
    _write_manifest(path, document)

    with pytest.raises(ValidationError, match="ambiguous catalog binding"):
        load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_rejects_invalid_remote_topology_before_provider_use(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    document = _remote_document()
    entries = document["entries"]
    assert isinstance(entries, list)
    entry = entries[0]
    assert isinstance(entry, dict)
    entry["gdb_port_max"] = 70000
    _write_manifest(path, document)

    with pytest.raises(ValidationError, match="gdb port range"):
        load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_absent_manifest_disables_system_authority(tmp_path: Path) -> None:
    assert (
        load_authority_system_manifest(
            tmp_path / "manifest.json", owner_uid=os.getuid(), owner_gid=os.getgid()
        )
        is None
    )


def test_local_installation_requires_every_private_directory_and_exact_base(tmp_path: Path) -> None:
    document = _local_document()
    document["bases"] = tuple(document["bases"])
    bases = document["bases"]
    assert isinstance(bases, tuple)
    base_document = bases[0]
    assert isinstance(base_document, dict)
    base_document["root_identity"] = "sha256:" + sha256(b"base").hexdigest()
    manifest = LocalAuthoritySystemManifestV1.model_validate(document)
    state = tmp_path / "system-provisioning"
    local = state / "local"
    remote = state / "remote"
    pool = tmp_path / "pool"
    for path in (
        state,
        state / "system-operations",
        local,
        local / "state",
        local / "state/intents",
        local / "rootfs",
        local / "rootfs/systems",
        local / "rootfs/baselines",
        local / "rootfs/bases",
    ):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = local / "rootfs/bases" / manifest.bases[0].filename
    base.write_bytes(b"base")
    base.chmod(0o400)

    validate_authority_system_installation(
        manifest,
        state_root=state,
        local_root=local,
        remote_root=remote,
        remote_pool_root=pool,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    base.chmod(0o600)
    with pytest.raises(ValueError, match="staged base is unsafe"):
        validate_authority_system_installation(
            manifest,
            state_root=state,
            local_root=local,
            remote_root=remote,
            remote_pool_root=pool,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_local_installation_rejects_base_with_wrong_root_identity(tmp_path: Path) -> None:
    document = _local_document()
    document["bases"] = tuple(document["bases"])
    manifest = LocalAuthoritySystemManifestV1.model_validate(document)
    state = tmp_path / "system-provisioning"
    local = state / "local"
    remote = state / "remote"
    pool = tmp_path / "pool"
    for path in (
        state,
        state / "system-operations",
        local,
        local / "state",
        local / "state/intents",
        local / "rootfs",
        local / "rootfs/systems",
        local / "rootfs/baselines",
        local / "rootfs/bases",
    ):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = local / "rootfs/bases" / manifest.bases[0].filename
    base.write_bytes(b"wrong-base")
    base.chmod(0o400)

    with pytest.raises(ValueError, match="root identity"):
        validate_authority_system_installation(
            manifest,
            state_root=state,
            local_root=local,
            remote_root=remote,
            remote_pool_root=pool,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_local_provider_rechecks_the_pinned_base_before_provisioning(tmp_path: Path) -> None:
    document = _local_document()
    document["bases"] = tuple(document["bases"])
    bases_document = document["bases"]
    assert isinstance(bases_document, tuple)
    entry_document = bases_document[0]
    assert isinstance(entry_document, dict)
    root_identity = "sha256:" + sha256(b"base").hexdigest()
    entry_document["root_identity"] = root_identity
    manifest = LocalAuthoritySystemManifestV1.model_validate(document)
    state = tmp_path / "system-provisioning"
    local = state / "local"
    for path in (
        state,
        state / "system-operations",
        local,
        local / "state",
        local / "state/intents",
        local / "rootfs",
        local / "rootfs/systems",
        local / "rootfs/baselines",
        local / "rootfs/bases",
    ):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = local / "rootfs/bases" / manifest.bases[0].filename
    base.write_bytes(b"base")
    base.chmod(0o400)
    verified = validate_authority_system_installation(
        manifest,
        state_root=state,
        local_root=local,
        remote_root=state / "remote",
        remote_pool_root=tmp_path / "pool",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    connection_calls: list[None] = []
    provider, close = build_local_authority_system_provider(
        manifest,
        base_files=verified,
        connect=lambda: connection_calls.append(None),
        state_root=local / "state",
        rootfs_root=local / "rootfs",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    profile = ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "linux-test",
            "provider": {
                "local-libvirt": {
                    "domain_xml_params": {},
                    "rootfs": {
                        "kind": "local",
                        "path": "/controller/base.qcow2",
                        "sha256": root_identity,
                    },
                }
            },
        }
    )
    system_id, allocation_id, resource_id = uuid4(), uuid4(), uuid4()
    bootstrap_public_key = "ssh-ed25519 YWFhYQ== kdive-system"
    snapshot = AuthoritySystemProvisionSnapshot(
        system_id=system_id,
        allocation_id=allocation_id,
        resource_id=resource_id,
        project="project-a",
        provider_kind="local-libvirt",
        resource_name="local-a",
        authority_instance="authority-a",
        profile=profile,
        profile_identity="sha256:" + profile_digest(profile),
        source_image_id=uuid4(),
        root_identity=root_identity,
        root_spec=RootSpecV1(
            architecture="x86_64",
            root="/dev/vda1",
            arguments=("root=/dev/vda1",),
            authority="stage-inspection",
            source={"kind": "staged-image", "identity": root_identity},
        ),
        bootstrap_public_key=bootstrap_public_key,
        bootstrap_identity="sha256:" + sha256(bootstrap_public_key.encode()).hexdigest(),
    )
    request = AuthoritySystemMutationRequestV1(
        system_id=system_id,
        allocation_id=allocation_id,
        resource_id=resource_id,
        provider_kind="local-libvirt",
        resource_name="local-a",
        authority_instance="authority-a",
        profile_identity=snapshot.profile_identity,
        root_identity=root_identity,
        operation=AuthoritySystemOperation.PROVISION,
        operation_identity="provision-a",
        authority_id=uuid4(),
        generation=1,
        attempt_id=uuid4(),
        operation_digest="sha256:" + "c" * 64,
        bootstrap_identity=snapshot.bootstrap_identity,
    )
    context = AuthoritySystemCommitContextV1(
        attempt_id=request.attempt_id,
        operation=AuthoritySystemOperation.PROVISION,
        journal_sequence=1,
        journal_digest="sha256:" + "d" * 64,
    )
    base.chmod(0o600)
    base.write_bytes(b"changed")
    base.chmod(0o400)
    try:
        with pytest.raises(ValueError, match="base metadata changed"):
            asyncio.run(provider.execute_system_provision(request, context, snapshot))
    finally:
        close()
    assert connection_calls == []
