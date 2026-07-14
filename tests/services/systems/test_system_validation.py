"""Systems profile validation tests."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from kdive.components.references import ROOTFS_COMPONENT, ComponentSourceKind
from kdive.components.validation import ComponentSourceCapabilities
from kdive.domain.catalog.resource_capabilities import GuestArch, resolve_accel_emulator
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.services.systems.validation import (
    _reject_unknown_destructive_ops,
    require_fadump_supported,
    resolve_accel,
    validate_profile_for_provider,
    validate_rootfs_for_provider,
)

_LOCAL_POLICY = LocalLibvirtProfilePolicy()
_FAULT_POLICY = FaultInjectProfilePolicy()

_VALID_PROFILE: dict[str, Any] = {
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
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def _profile(rootfs: dict[str, object] | None = None) -> ProvisioningProfile:
    data = copy.deepcopy(_VALID_PROFILE)
    if rootfs is not None:
        data["provider"]["local-libvirt"]["rootfs"] = rootfs
    return ProvisioningProfile.parse(data)


def _capabilities(*accepted_rootfs_sources: ComponentSourceKind) -> ComponentSourceCapabilities:
    return ComponentSourceCapabilities(
        provider="local-libvirt",
        accepted_component_sources={ROOTFS_COMPONENT: frozenset(accepted_rootfs_sources)},
    )


def test_validate_profile_for_provider_accepts_advertised_rootfs_source() -> None:
    validate_profile_for_provider(_profile(), _LOCAL_POLICY, _capabilities("local"))


def test_validate_profile_for_provider_rejects_unsupported_rootfs_source() -> None:
    with pytest.raises(CategorizedError) as exc_info:
        validate_profile_for_provider(_profile(), _LOCAL_POLICY, _capabilities("catalog"))

    error = exc_info.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details == {
        "provider": "local-libvirt",
        "component_kind": "rootfs",
        "source_kind": "local",
        "accepted_source_kinds": ["catalog"],
    }


def test_validate_profile_for_provider_runs_static_profile_validation_first() -> None:
    profile = _profile()
    data = profile.model_dump(mode="json", by_alias=True)
    data["provider"]["local-libvirt"]["domain_xml_params"] = {"unsupported": "value"}
    invalid_profile = ProvisioningProfile.parse(data)

    with pytest.raises(CategorizedError) as exc_info:
        validate_profile_for_provider(invalid_profile, _LOCAL_POLICY, _capabilities("local"))

    error = exc_info.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details == {
        "supported": ["machine"],
        "unsupported": ["unsupported"],
    }
    # The message names the offending key so an operator can find the typo in the profile.
    assert str(error) == "unsupported domain_xml_params: unsupported"


def test_unsupported_domain_xml_params_message_lists_all_sorted_comma_joined() -> None:
    profile = _profile()
    data = profile.model_dump(mode="json", by_alias=True)
    # Two unknown keys: the message must list both, sorted, comma-and-space joined.
    data["provider"]["local-libvirt"]["domain_xml_params"] = {"zeta": "1", "alpha": "2"}
    invalid_profile = ProvisioningProfile.parse(data)

    with pytest.raises(CategorizedError) as exc_info:
        validate_profile_for_provider(invalid_profile, _LOCAL_POLICY, _capabilities("local"))

    error = exc_info.value
    assert error.details["unsupported"] == ["alpha", "zeta"]
    assert str(error) == "unsupported domain_xml_params: alpha, zeta"


def test_validate_profile_validates_the_profiles_own_catalog_rootfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A systems.toml declares one image; the profile's catalog rootfs names a different,
    # undeclared image. validate_profile must validate the profile's OWN rootfs reference,
    # so the undeclared name is rejected.
    inventory = tmp_path / "systems.toml"
    inventory.write_text(
        "schema_version = 2\n\n"
        "[[image]]\n"
        'provider = "local-libvirt"\n'
        'name = "declared-image"\n'
        'arch = "x86_64"\n'
        'format = "qcow2"\n'
        'root_device = "/dev/vda"\n'
        'visibility = "public"\n'
        'capabilities = ["agent"]\n'
        "[image.source]\n"
        'kind = "s3"\n'
        'object_key = "rootfs/local/declared.qcow2"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(inventory))
    profile = _profile({"kind": "catalog", "provider": "local-libvirt", "name": "undeclared-name"})

    with pytest.raises(CategorizedError) as exc_info:
        _LOCAL_POLICY.validate_profile(profile)

    error = exc_info.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    # The rejection enumerates the declared (provider, name) set so a black-box caller can
    # self-correct (#731, ADR-0224). The exact-dict assertion is itself the no-leak guard: only
    # the catalog identity is present, never the inventory source's object_key (ADR-0123).
    assert error.details == {
        "provider": "local-libvirt",
        "name": "undeclared-name",
        "available": ["local-libvirt/declared-image"],
    }


def test_validate_rootfs_for_provider_invokes_validator_for_regular_rootfs() -> None:
    calls: list[RootfsSource] = []

    def validate(rootfs: RootfsSource) -> None:
        calls.append(rootfs)

    asyncio.run(validate_rootfs_for_provider(_profile(), _LOCAL_POLICY, validate))

    assert [rootfs.kind for rootfs in calls] == ["local"]


def test_validate_rootfs_for_provider_skips_upload_rootfs() -> None:
    def fail_on_call(_: RootfsSource) -> None:
        raise AssertionError("upload-kind rootfs is system-owned and not provider-validated")

    asyncio.run(
        validate_rootfs_for_provider(_profile({"kind": "upload"}), _LOCAL_POLICY, fail_on_call)
    )
    validate_profile_for_provider(_profile({"kind": "upload"}), _LOCAL_POLICY, _capabilities())


def _profile_with_ops(destructive_ops: list[str]) -> ProvisioningProfile:
    data = copy.deepcopy(_VALID_PROFILE)
    data["provider"]["local-libvirt"]["destructive_ops"] = destructive_ops
    return ProvisioningProfile.parse(data)


def test_reject_unknown_destructive_ops_flags_typo_directly() -> None:
    with pytest.raises(CategorizedError) as exc:
        _reject_unknown_destructive_ops(_profile_with_ops(["force-crash"]))  # hyphen typo
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["unknown_destructive_ops"] == ["force-crash"]
    # The message names the offending field so an operator can find the typo, and the
    # details advertise the exact closed set of accepted tokens under a stable key.
    assert str(exc.value) == "provisioning profile declares unknown destructive_ops tokens"
    # Only force_crash opts in now (ADR-0326): power is contributor lifecycle, teardown gates by
    # role only (ADR-0129), and reprovision became contributor leaseholder control (ADR-0326) —
    # all three are rejected as non-gating tokens.
    assert exc.value.details["valid_destructive_ops"] == ["force_crash"]


def test_reject_unknown_destructive_ops_accepts_known_directly() -> None:
    _reject_unknown_destructive_ops(_profile_with_ops(["force_crash"]))


@pytest.mark.parametrize("token", ["power", "teardown", "reprovision"])
def test_reject_unknown_destructive_ops_rejects_non_opt_in_tokens(token: str) -> None:
    # power (contributor lifecycle), teardown (role-only gate), and reprovision (contributor
    # leaseholder control, ADR-0326) no longer opt into anything via destructive_ops, so listing
    # any of them is a rejected token.
    with pytest.raises(CategorizedError) as exc:
        _reject_unknown_destructive_ops(_profile_with_ops([token]))
    assert exc.value.details["unknown_destructive_ops"] == [token]
    assert exc.value.details["valid_destructive_ops"] == ["force_crash"]


def test_validate_profile_for_provider_rejects_unknown_token() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_profile_for_provider(
            _profile_with_ops(["powercycle"]), _LOCAL_POLICY, _capabilities("local")
        )
    assert exc.value.details["unknown_destructive_ops"] == ["powercycle"]


def test_validate_profile_for_provider_accepts_known_tokens() -> None:
    validate_profile_for_provider(
        _profile_with_ops(["force_crash"]), _LOCAL_POLICY, _capabilities("local")
    )


def test_validate_rootfs_for_provider_propagates_validator_error() -> None:
    def reject(_: RootfsSource) -> None:
        raise CategorizedError(
            "rootfs path is outside allowed roots",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"path": "/tmp/rootfs.qcow2"},
        )

    with pytest.raises(CategorizedError) as exc_info:
        asyncio.run(validate_rootfs_for_provider(_profile(), _LOCAL_POLICY, reject))

    assert exc_info.value.details == {"path": "/tmp/rootfs.qcow2"}


def test_validate_rootfs_for_provider_skips_providers_without_rootfs() -> None:
    data = copy.deepcopy(_VALID_PROFILE)
    data["provider"] = {"fault-inject": {"capture_method": "console"}}
    profile = ProvisioningProfile.parse(data)

    def fail_on_call(_: RootfsSource) -> None:
        pytest.fail("fault-inject profiles do not expose a provider rootfs")

    asyncio.run(validate_rootfs_for_provider(profile, _FAULT_POLICY, fail_on_call))
    validate_profile_for_provider(profile, _FAULT_POLICY, _capabilities())


# guest_arches mappings as ResourceCapabilities.guest_arches() returns them (ADR-0338/0339).
_X86_HOST: dict[str, GuestArch] = {
    "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"},
    "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"},
}
_PPC_HOST: dict[str, GuestArch] = {
    "x86_64": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-x86_64"},
    "ppc64le": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-ppc64"},
}


@pytest.mark.parametrize(
    ("guest_arches", "arch", "expected_accel"),
    [
        (_X86_HOST, "x86_64", "kvm"),
        (_X86_HOST, "ppc64le", "tcg"),
        (_PPC_HOST, "ppc64le", "kvm"),
        (_PPC_HOST, "x86_64", "tcg"),
    ],
)
def test_resolve_accel_returns_advertised_accelerator(
    guest_arches: dict[str, GuestArch], arch: str, expected_accel: str
) -> None:
    assert resolve_accel(guest_arches, arch) == expected_accel


@pytest.mark.parametrize("arch", ["x86_64", "ppc64le"])
def test_resolve_accel_fails_open_on_empty_guest_arches(arch: str) -> None:
    # A resource advertising no guest arches (remote-libvirt / fault-inject / a host not
    # re-discovered since ADR-0338) skips the check and records no accelerator.
    assert resolve_accel({}, arch) is None


def test_resolve_accel_rejects_unadvertised_arch_naming_supported_set() -> None:
    ppc_only: dict[str, GuestArch] = {
        "ppc64le": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-ppc64"}
    }

    with pytest.raises(CategorizedError) as exc_info:
        resolve_accel(ppc_only, "x86_64")

    exc = exc_info.value
    assert exc.category is ErrorCategory.CONFIGURATION_ERROR
    assert "x86_64" in str(exc)
    assert "ppc64le" in str(exc)  # the supported set is named in the message
    assert exc.details == {"requested_arch": "x86_64", "accepted_values": ["ppc64le"]}


def test_require_fadump_supported_noop_when_not_requested() -> None:
    # A non-fadump provision is never gated, regardless of the host signal (ADR-0349).
    require_fadump_supported(requested=False, supported=False)
    require_fadump_supported(requested=False, supported=True)


def test_require_fadump_supported_passes_when_requested_and_supported() -> None:
    require_fadump_supported(requested=True, supported=True)


def test_require_fadump_supported_rejects_when_requested_but_unsupported() -> None:
    # Fail-closed: a fadump-opted provision on an unsupporting host is a CONFIGURATION_ERROR
    # naming the QEMU floor, raised before any capacity is debited (never a hang).
    with pytest.raises(CategorizedError) as exc:
        require_fadump_supported(requested=True, supported=False)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "10.2" in str(exc.value)


@pytest.mark.parametrize(
    ("guest_arches", "arch"),
    [
        ({}, "x86_64"),  # empty -> both fail open
        (_X86_HOST, "x86_64"),  # present -> both resolve
        (_X86_HOST, "ppc64le"),  # present -> both resolve
        (_X86_HOST, "s390x"),  # absent -> both fail closed
    ],
)
def test_resolve_accel_and_resolve_accel_emulator_agree(
    guest_arches: dict[str, GuestArch], arch: str
) -> None:
    # Bind the two resolution sites (admission's resolve_accel and the provider's
    # resolve_accel_emulator) so a future change to one cannot silently diverge the other:
    # they must agree on which of the three branches (open / resolved / raise) fires.
    accel_raised = None
    pair_raised = None
    accel_result = None
    pair_result = None
    try:
        accel_result = resolve_accel(guest_arches, arch)
    except CategorizedError as exc:
        accel_raised = exc.category
    try:
        pair_result = resolve_accel_emulator(guest_arches, arch)
    except CategorizedError as exc:
        pair_raised = exc.category

    assert accel_raised == pair_raised
    if accel_raised is None:
        # resolve_accel returns the accel (or None); resolve_accel_emulator returns (accel, _)
        # or None. The accel component must match.
        pair_accel = pair_result[0] if pair_result is not None else None
        assert accel_result == pair_accel
