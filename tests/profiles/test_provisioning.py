"""Tests for the provisioning-profile schema (`kdive.profiles.provisioning`)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import BootMethod, ProvisioningProfile

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
            "rootfs_image_ref": "oci://registry.internal/rootfs/fedora-40@sha256:abc123",
            "crashkernel": "256M",
        }
    },
}


def _valid() -> dict[str, Any]:
    """A fresh deep copy of the canonical valid profile, safe to mutate."""
    return copy.deepcopy(_VALID)


def test_valid_libvirt_profile_parses() -> None:
    profile = ProvisioningProfile.parse(_valid())

    assert profile.schema_version == 1
    assert profile.arch == "x86_64"
    assert profile.vcpu == 4
    assert profile.memory_mb == 4096
    assert profile.disk_gb == 20
    assert profile.boot_method is BootMethod.DIRECT_KERNEL
    assert profile.kernel_source_ref.startswith("git+https://")
    assert profile.provider.local_libvirt.domain_xml_params == {"machine": "pc-q35-9.0"}
    assert profile.provider.local_libvirt.rootfs_image_ref.startswith("oci://")


def test_crashkernel_is_present() -> None:
    # The crashkernel reservation is the kdump prerequisite (acceptance criterion).
    profile = ProvisioningProfile.parse(_valid())

    assert profile.provider.local_libvirt.crashkernel == "256M"


def _expect_configuration_error(data: dict[str, Any]) -> None:
    """Assert that parsing ``data`` fails as a CONFIGURATION_ERROR."""
    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(data)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "arch",
        "vcpu",
        "memory_mb",
        "disk_gb",
        "boot_method",
        "kernel_source_ref",
        "provider",
    ],
)
def test_missing_core_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data[field]
    _expect_configuration_error(data)


@pytest.mark.parametrize("field", ["rootfs_image_ref", "crashkernel"])
def test_missing_libvirt_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data["provider"]["local-libvirt"][field]
    _expect_configuration_error(data)


def test_unknown_top_level_field_rejected() -> None:
    data = _valid()
    data["unexpected"] = "x"
    _expect_configuration_error(data)


def test_unknown_provider_key_rejected() -> None:
    data = _valid()
    data["provider"]["cloud"] = {}
    _expect_configuration_error(data)


def test_unknown_libvirt_field_rejected() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["extra"] = "x"
    _expect_configuration_error(data)
