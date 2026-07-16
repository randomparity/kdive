"""The mint-time ``resolved_cpu`` snapshot is remote-only (#1227, ADR-0369).

Extending ``host_cpu`` advertisement to local-libvirt (Phase A) must not make the shared mint path
snapshot the native host CPU onto a local System: local ``resolved_cpu`` is a live read (Phase C),
and a mint snapshot would be wrong for a CPU pin and arch-mismatched for a foreign-TCG guest. The
gate is exercised here as a pure helper (no DB) against local/remote/fault profiles.
"""

from __future__ import annotations

from typing import Any

from kdive.domain.catalog.resource_capabilities import ResourceCapabilities
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.services.systems.admission import _mint_resolved_cpu

_HOST_CPU = {
    "model": "Skylake-Client-IBRS",
    "vendor": "Intel",
    "arch": "x86_64",
    "baseline_level": "x86-64-v3",
}


def _profile(provider: dict[str, Any], boot_method: str) -> ProvisioningProfile:
    data: dict[str, Any] = {
        "schema_version": 1,
        "arch": "x86_64",
        "boot_method": boot_method,
        "provider": provider,
    }
    if boot_method == "direct-kernel":
        data["kernel_source_ref"] = "git+https://git.kernel.org/pub/scm/linux.git#v6.9"
    return ProvisioningProfile.model_validate(data)


def _local_profile() -> ProvisioningProfile:
    return _profile({"local-libvirt": {"rootfs": {"kind": "upload"}}}, "direct-kernel")


def _remote_profile() -> ProvisioningProfile:
    return _profile({"remote-libvirt": {"base_image_volume": "base.qcow2"}}, "disk-image")


def _fault_profile() -> ProvisioningProfile:
    return _profile({"fault-inject": {}}, "direct-kernel")


def _caps(host_cpu: dict[str, Any] | None) -> ResourceCapabilities:
    return ResourceCapabilities.from_mapping({"host_cpu": host_cpu} if host_cpu else {})


def test_local_profile_never_snapshots_host_cpu() -> None:
    # Even when the bound host advertises host_cpu, a LOCAL mint records None (Phase C is source).
    assert _mint_resolved_cpu(_local_profile(), _caps(_HOST_CPU)) is None


def test_fault_profile_never_snapshots_host_cpu() -> None:
    assert _mint_resolved_cpu(_fault_profile(), _caps(_HOST_CPU)) is None


def test_remote_profile_snapshots_host_cpu() -> None:
    assert _mint_resolved_cpu(_remote_profile(), _caps(_HOST_CPU)) == _HOST_CPU


def test_remote_profile_none_when_host_advertises_none() -> None:
    assert _mint_resolved_cpu(_remote_profile(), _caps(None)) is None


def test_remote_profile_none_when_caps_absent() -> None:
    assert _mint_resolved_cpu(_remote_profile(), None) is None
