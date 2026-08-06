"""Profile provider section vs. the Resource kind it is provisioned against (#1899, ADR-0549).

The provider runtime — hence the ``ProfilePolicy`` — is resolved from the Resource, so a profile
whose section names a different provider than the policy is a mismatch that must be rejected with
a typed ``configuration_error`` rather than a bare ``AttributeError`` (or, for fault-inject,
silently accepted).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from kdive.components.references import ROOTFS_COMPONENT
from kdive.components.validation import ComponentSourceCapabilities
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy
from kdive.services.systems.validation import validate_profile_for_provider

_POLICIES: dict[ResourceKind, ProfilePolicy] = {
    ResourceKind.LOCAL_LIBVIRT: LocalLibvirtProfilePolicy(),
    ResourceKind.REMOTE_LIBVIRT: RemoteLibvirtProfilePolicy(),
    ResourceKind.FAULT_INJECT: FaultInjectProfilePolicy(),
}

_SECTIONS: dict[ResourceKind, dict[str, Any]] = {
    ResourceKind.LOCAL_LIBVIRT: {
        "local-libvirt": {
            "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"}
        }
    },
    # ADR-0080 pairs the remote-libvirt section with boot_method 'disk-image' biconditionally,
    # so this section forces that boot_method in _profile below.
    ResourceKind.REMOTE_LIBVIRT: {
        "remote-libvirt": {"base_image_volume": "kdive-base-fedora-42.qcow2"}
    },
    ResourceKind.FAULT_INJECT: {"fault-inject": {"capture_method": "console"}},
}

_MISMATCHES = [
    (policy_kind, profile_kind)
    for policy_kind in _POLICIES
    for profile_kind in _SECTIONS
    if policy_kind is not profile_kind
]


def _profile(kind: ResourceKind, **overrides: Any) -> ProvisioningProfile:
    data: dict[str, Any] = {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "provider": copy.deepcopy(_SECTIONS[kind]),
    }
    if kind is ResourceKind.REMOTE_LIBVIRT:
        data["boot_method"] = "disk-image"
    else:
        data["boot_method"] = "direct-kernel"
        data["kernel_source_ref"] = "git+https://git.kernel.org/pub/scm/linux.git#v6.9"
    data.update(overrides)
    return ProvisioningProfile.parse(data)


def _capabilities() -> ComponentSourceCapabilities:
    # Permissive on purpose: the component-source gate must not be what rejects a mismatch.
    return ComponentSourceCapabilities(
        provider="test-provider",
        accepted_component_sources={ROOTFS_COMPONENT: frozenset({"local", "catalog"})},
    )


@pytest.mark.parametrize(("policy_kind", "profile_kind"), _MISMATCHES)
def test_mismatched_profile_section_is_a_configuration_error(
    policy_kind: ResourceKind, profile_kind: ResourceKind
) -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_profile_for_provider(
            _profile(profile_kind), _POLICIES[policy_kind], _capabilities()
        )

    error = caught.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    # The message must name BOTH sides, so the agent can tell which one to change.
    assert profile_kind.value in str(error)
    assert policy_kind.value in str(error)
    assert error.details == {
        "profile_provider_section": profile_kind.value,
        "resource_kind": policy_kind.value,
    }


@pytest.mark.parametrize("kind", list(_POLICIES))
def test_matching_profile_section_is_accepted(kind: ResourceKind) -> None:
    # The happy path keeps the mismatch guard from passing vacuously: a policy that declared the
    # wrong kind would redden here rather than silently reject everything. It pins the kind check
    # only — for remote-libvirt and fault-inject the rest of the function short-circuits.
    validate_profile_for_provider(_profile(kind), _POLICIES[kind], _capabilities())


@pytest.mark.parametrize("kind", list(_POLICIES))
def test_each_policy_declares_the_kind_whose_section_it_owns(kind: ResourceKind) -> None:
    assert _POLICIES[kind].kind is kind


def test_kind_mismatch_is_reported_before_the_destructive_ops_token_check() -> None:
    # Ordering is load-bearing (ADR-0549): the cross-check runs before any provider dereference,
    # which is what makes the bare-AttributeError paths unreachable for a mismatch. A profile that
    # is both mismatched and carries a bogus token must surface the mismatch, not the token.
    profile = _profile(ResourceKind.FAULT_INJECT)
    data = profile.model_dump(mode="json", by_alias=True)
    data["provider"]["fault-inject"]["destructive_ops"] = ["force-crash"]  # hyphen typo

    with pytest.raises(CategorizedError) as caught:
        validate_profile_for_provider(
            ProvisioningProfile.parse(data), _POLICIES[ResourceKind.LOCAL_LIBVIRT], _capabilities()
        )

    assert caught.value.details["profile_provider_section"] == "fault-inject"


def test_fault_inject_policy_no_longer_accepts_a_libvirt_profile_outright() -> None:
    # The regression this issue was filed for: fault-inject's rootfs_source returns None and its
    # validate_profile was a no-op, so a libvirt-section profile passed admission COMPLETELY and
    # was persisted; the AttributeError was deferred to destructive_opt_in/capture_method.
    with pytest.raises(CategorizedError):
        validate_profile_for_provider(
            _profile(ResourceKind.LOCAL_LIBVIRT),
            _POLICIES[ResourceKind.FAULT_INJECT],
            _capabilities(),
        )
