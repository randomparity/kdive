"""Repository validation for authority-owned System snapshots."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest

from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.providers.ports.external_boot import RootSpecV1
from kdive.providers.system_authority.repository import AuthoritySystemBinding, _snapshot


def _profile() -> ProvisioningProfile:
    return ProvisioningProfile.parse(
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
                    "rootfs": {"kind": "local", "path": "/configured/root.qcow2"},
                }
            },
        }
    )


def test_snapshot_recomputes_persisted_profile_root_and_bootstrap_identities() -> None:
    profile = _profile()
    root_digest = "sha256:" + "a" * 64
    public_key = "ssh-ed25519 YWFhYQ== kdive-system"
    binding = AuthoritySystemBinding(
        authority_id=uuid4(),
        generation=1,
        system_id=uuid4(),
        allocation_id=uuid4(),
        resource_id=uuid4(),
        provider_kind="local-libvirt",
        resource_name="host-a",
        authority_instance="auth-a",
        profile_identity="sha256:" + profile_digest(profile),
        root_identity=root_digest,
        bootstrap_identity="sha256:" + hashlib.sha256(public_key.encode()).hexdigest(),
        operation="provision",
        operation_identity="provision-a",
        operation_digest="sha256:" + "b" * 64,
        state="current",
    )
    root = RootSpecV1(
        architecture="x86_64",
        root="/dev/vda2",
        arguments=("root=/dev/vda2",),
        authority="stage-inspection",
        source={"kind": "staged-image", "identity": root_digest},
    )
    row = {
        "provisioning_profile": profile.model_dump(mode="json", by_alias=True, exclude_none=True),
        "root_spec": root.model_dump(mode="json", by_alias=True),
        "root_architecture": "x86_64",
        "project": "proj",
        "source_image_id": uuid4(),
        "bootstrap_public_key": public_key,
    }
    snapshot = _snapshot(row, binding)
    assert snapshot.profile == profile
    assert snapshot.root_spec == root

    with pytest.raises(ValueError, match="profile identity"):
        _snapshot(row, replace(binding, profile_identity=root_digest))
