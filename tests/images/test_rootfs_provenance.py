"""Rootfs provenance serialization contract."""

from __future__ import annotations

from kdive.domain.catalog.images import Capability
from kdive.images.planes.base import (
    PROVENANCE_BOOT_KERNEL_COUNT,
    PROVENANCE_DEFAULT_KERNEL_VERSION,
    PROVENANCE_DRGN_VERSION,
    PROVENANCE_MAKEDUMPFILE_VERSION,
    PROVENANCE_OS_RELEASE,
    RootfsBuildProvenance,
    RootfsBuildSpec,
)


def _spec() -> RootfsBuildSpec:
    return RootfsBuildSpec(
        provider="local-libvirt",
        name="fedora-kdive-ready-43",
        arch="x86_64",
        releasever="43",
        packages=("openssh-server", "drgn"),
        source_image_digest="sha256:source",
        capabilities=(Capability.AGENT, Capability.KDUMP),
    )


def test_local_provenance_serializes_optional_operands_and_keeps_zero_count() -> None:
    provenance = RootfsBuildProvenance.local_libvirt(
        _spec(),
        source_image_digest="virt-builder:fedora-43",
        image_size="6G",
        readiness_marker="kdive-ready",
        layout="whole-disk-ext4-qcow2",
        guest_mac="selinux-permissive",
        package_versions={"drgn": "0.0.28"},
        makedumpfile_version="1.7.9",
        drgn_version="0.0.31",
        boot_kernel_count=0,
        default_kernel_version="",
        os_release={"id": "fedora", "version_id": "43"},
    ).to_dict()

    assert provenance["plane"] == "local-libvirt"
    assert provenance["packages"] == ["openssh-server", "drgn"]
    assert provenance["capabilities"] == ["agent", "kdump"]
    assert provenance[PROVENANCE_MAKEDUMPFILE_VERSION] == "1.7.9"
    assert provenance[PROVENANCE_DRGN_VERSION] == "0.0.31"
    assert provenance[PROVENANCE_BOOT_KERNEL_COUNT] == 0
    assert PROVENANCE_DEFAULT_KERNEL_VERSION not in provenance
    assert provenance[PROVENANCE_OS_RELEASE] == {"id": "fedora", "version_id": "43"}


def test_local_provenance_omits_drgn_version_when_absent() -> None:
    provenance = RootfsBuildProvenance.local_libvirt(
        _spec(),
        source_image_digest="virt-builder:fedora-43",
        image_size="6G",
        readiness_marker="kdive-ready",
        layout="whole-disk-ext4-qcow2",
        guest_mac="selinux-permissive",
        package_versions={},
        makedumpfile_version=None,
        drgn_version=None,
        boot_kernel_count=None,
        default_kernel_version=None,
        os_release=None,
    ).to_dict()

    assert PROVENANCE_DRGN_VERSION not in provenance


def test_remote_provenance_omits_absent_optional_operands() -> None:
    provenance = RootfsBuildProvenance.remote_libvirt(
        _spec(),
        packages=("qemu-guest-agent", "openssh-server", "drgn"),
        image_size="10G",
        boot_method="disk-image",
        guest_access_seam="qemu-guest-agent",
        package_versions={},
    ).to_dict()

    assert provenance == {
        "plane": "remote-libvirt",
        "boot_method": "disk-image",
        "releasever": "43",
        "packages": ["qemu-guest-agent", "openssh-server", "drgn"],
        "source_image_digest": "sha256:source",
        "capabilities": ["agent", "kdump"],
        "arch": "x86_64",
        "image_size": "10G",
        "guest_access_seam": "qemu-guest-agent",
    }
