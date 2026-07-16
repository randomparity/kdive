"""Admission validates a CPU pin against the host's per-arch ``selectable_cpus`` (#1227, ADR-0369).

Host-deliverability only (not image ISA compatibility): a pin absent from the bound host's
``selectable_cpus[profile.arch]`` is rejected ``CONFIGURATION_ERROR`` at mint (fail-closed — never
render a custom ``<cpu>`` the host cannot be shown to support). The check is a pure helper here
(no DB), exercised against local/remote/fault profiles.
"""

from __future__ import annotations

from typing import Any

import pytest

from kdive.domain.catalog.resource_capabilities import ResourceCapabilities
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.services.systems.admission import require_pinned_cpu_selectable

_SELECTABLE = {"x86_64": ["qemu64", "x86-64-v2"], "ppc64le": ["POWER10"]}


def _profile(
    provider: dict[str, Any], boot_method: str, arch: str = "x86_64"
) -> ProvisioningProfile:
    data: dict[str, Any] = {
        "schema_version": 1,
        "arch": arch,
        "boot_method": boot_method,
        "provider": provider,
    }
    if boot_method == "direct-kernel":
        data["kernel_source_ref"] = "git+https://git.kernel.org/pub/scm/linux.git#v6.9"
    return ProvisioningProfile.model_validate(data)


def _local(arch: str, model: str | None) -> ProvisioningProfile:
    section: dict[str, Any] = {"rootfs": {"kind": "upload"}}
    if model is not None:
        section["cpu"] = {"model": model}
    return _profile({"local-libvirt": section}, "direct-kernel", arch)


def _caps() -> ResourceCapabilities:
    return ResourceCapabilities.from_mapping({"selectable_cpus": _SELECTABLE})


def test_pin_in_arch_set_accepted() -> None:
    require_pinned_cpu_selectable(_local("x86_64", "x86-64-v2"), _caps())  # no raise


def test_unpinned_profile_no_check() -> None:
    require_pinned_cpu_selectable(_local("x86_64", None), _caps())  # no raise


def test_pin_not_in_arch_set_rejected() -> None:
    with pytest.raises(CategorizedError) as exc:
        require_pinned_cpu_selectable(_local("x86_64", "Skylake-Client"), _caps())
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "Skylake-Client" in str(exc.value)
    assert "x86_64" in str(exc.value)


def test_pin_in_other_arch_set_rejected() -> None:
    # POWER10 is selectable for ppc64le, not x86_64 — a wrong-arch pin is rejected.
    with pytest.raises(CategorizedError):
        require_pinned_cpu_selectable(_local("x86_64", "POWER10"), _caps())


def test_pin_when_arch_has_no_set_rejected() -> None:
    empty = ResourceCapabilities.from_mapping({})
    with pytest.raises(CategorizedError):
        require_pinned_cpu_selectable(_local("x86_64", "x86-64-v2"), empty)


def test_remote_profile_no_check() -> None:
    remote = _profile({"remote-libvirt": {"base_image_volume": "base.qcow2"}}, "disk-image")
    require_pinned_cpu_selectable(remote, _caps())  # no local section -> no raise
