"""Local-libvirt ProfilePolicy predicates for the #747 live-attach seam (ADR-0233)."""

from __future__ import annotations

import copy
from typing import Any

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy

_VALID: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "pc-q35-9.0"},
            "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"},
        }
    },
}


def _profile(
    *, gdbstub: bool = False, preserve_on_crash: bool = False, crashkernel: str | None = None
) -> ProvisioningProfile:
    data = copy.deepcopy(_VALID)
    section = data["provider"]["local-libvirt"]
    section["debug"] = {"gdbstub": gdbstub, "preserve_on_crash": preserve_on_crash}
    if crashkernel is not None:
        section["crashkernel"] = crashkernel
    return ProvisioningProfile.parse(data)


def test_gdbstub_provisioned_true_when_flag_set() -> None:
    assert LocalLibvirtProfilePolicy().gdbstub_provisioned(_profile(gdbstub=True)) is True


def test_gdbstub_provisioned_true_even_when_kdump_is_primary() -> None:
    # capture_method would report KDUMP here; gdbstub_provisioned must not be masked by it.
    profile = _profile(gdbstub=True, crashkernel="256M")
    assert LocalLibvirtProfilePolicy().gdbstub_provisioned(profile) is True


def test_gdbstub_provisioned_false_when_flag_unset() -> None:
    assert LocalLibvirtProfilePolicy().gdbstub_provisioned(_profile(gdbstub=False)) is False


def test_host_dump_provisioned_tracks_preserve_on_crash() -> None:
    policy = LocalLibvirtProfilePolicy()
    assert policy.host_dump_provisioned(_profile(gdbstub=True, preserve_on_crash=True)) is True
    assert policy.host_dump_provisioned(_profile(gdbstub=True, preserve_on_crash=False)) is False
