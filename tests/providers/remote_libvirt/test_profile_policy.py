"""Remote-libvirt ProfilePolicy predicates, focused on the host_dump opt-in (#1425, ADR-0426)."""

from __future__ import annotations

from typing import Any

from kdive.domain.capture import CaptureMethod
from kdive.jobs.handlers.runs.boot_evidence import available_capture, inert_capture
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy


def _remote_profile(**section_overrides: Any) -> ProvisioningProfile:
    section: dict[str, Any] = {
        "base_image_volume": "kdive-base-fedora-42.qcow2",
        **section_overrides,
    }
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 4,
            "memory_mb": 4096,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "provider": {"remote-libvirt": section},
        }
    )


def test_rootfs_source_none_for_operator_staged_volume() -> None:
    # ADR-0440: an operator-staged base_image_volume System has no rootfs component source, so the
    # component-source gate short-circuits (its pre-#1433 behavior).
    policy = RemoteLibvirtProfilePolicy()
    assert policy.rootfs_source(_remote_profile()) is None


def test_rootfs_source_returns_supplied_base_image_source() -> None:
    # ADR-0440 (#1433): a supplied base_image_source surfaces as the ROOTFS component source ref,
    # activating reject_unsupported_component_source at admission.
    policy = RemoteLibvirtProfilePolicy()
    profile = ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 4,
            "memory_mb": 4096,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "provider": {
                "remote-libvirt": {
                    "base_image_source": {
                        "kind": "local",
                        "path": "/var/lib/kdive/rootfs/fedora-44.qcow2",
                    }
                }
            },
        }
    )
    source = policy.rootfs_source(profile)
    assert source is not None
    assert source.kind == "local"
    assert source.path == "/var/lib/kdive/rootfs/fedora-44.qcow2"


def test_host_dump_defaults_off() -> None:
    policy = RemoteLibvirtProfilePolicy()
    assert policy.host_dump_provisioned(_remote_profile()) is False
    assert _remote_profile().provider.remote_libvirt.host_dump is False


def test_host_dump_provisioned_tracks_flag() -> None:
    policy = RemoteLibvirtProfilePolicy()
    assert policy.host_dump_provisioned(_remote_profile(host_dump=True)) is True
    assert policy.host_dump_provisioned(_remote_profile(host_dump=False)) is False


def test_available_capture_includes_host_dump_when_opted_in() -> None:
    policy = RemoteLibvirtProfilePolicy()
    methods = available_capture(policy, _remote_profile(host_dump=True))
    assert CaptureMethod.HOST_DUMP.value in methods
    # gdbstub is unconditionally provisioned on remote, so it is always offered.
    assert CaptureMethod.GDBSTUB.value in methods


def test_available_capture_omits_host_dump_when_unset() -> None:
    policy = RemoteLibvirtProfilePolicy()
    methods = available_capture(policy, _remote_profile())
    assert CaptureMethod.HOST_DUMP.value not in methods
    assert methods == [CaptureMethod.GDBSTUB.value, CaptureMethod.CONSOLE.value]


def test_inert_capture_reports_host_dump_when_opted_in() -> None:
    policy = RemoteLibvirtProfilePolicy()
    inert = inert_capture(policy, _remote_profile(host_dump=True))
    assert CaptureMethod.HOST_DUMP.value in inert


def test_inert_capture_omits_host_dump_when_unset() -> None:
    policy = RemoteLibvirtProfilePolicy()
    inert = inert_capture(policy, _remote_profile())
    assert CaptureMethod.HOST_DUMP.value not in inert


def test_host_dump_opt_in_leaves_capture_method_unchanged() -> None:
    # host_dump is a retrieve-path authorization, not a capture-method selector: with a
    # crashkernel present the resolved method stays KDUMP; without one it stays GDBSTUB.
    policy = RemoteLibvirtProfilePolicy()
    assert policy.capture_method(_remote_profile(host_dump=True)) is CaptureMethod.GDBSTUB
    assert (
        policy.capture_method(_remote_profile(host_dump=True, crashkernel="256M"))
        is CaptureMethod.KDUMP
    )
