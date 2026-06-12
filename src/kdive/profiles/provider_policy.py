"""Provider policy interface for parsed provisioning profiles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DestructiveJobKind, ResourceKind
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy


class ProfilePolicy(Protocol):
    """Provider-owned behavior derived from a parsed provisioning profile."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource | None:
        """Return the rootfs source used by this provider, if any."""

    def ssh_credential_ref(self, profile: ProvisioningProfile) -> str | None:
        """Return the live-SSH credential reference used by this provider, if any."""

    def drgn_live_requires_credential(self, profile: ProvisioningProfile) -> bool:
        """Return whether drgn-live needs a profile credential."""

    def validate_profile(self, profile: ProvisioningProfile) -> None:
        """Run provider-specific static profile validation."""

    def destructive_opt_in(self, profile: ProvisioningProfile, op: DestructiveJobKind) -> bool:
        """Return whether the profile opts into a destructive operation."""

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        """Resolve the crash-capture method enabled by the profile."""


_POLICIES: dict[ResourceKind, ProfilePolicy] = {
    ResourceKind.LOCAL_LIBVIRT: LocalLibvirtProfilePolicy(),
    ResourceKind.FAULT_INJECT: FaultInjectProfilePolicy(),
    ResourceKind.REMOTE_LIBVIRT: RemoteLibvirtProfilePolicy(),
}


def policy_for_profile(profile: ProvisioningProfile) -> ProfilePolicy:
    """Return the provider-owned policy adapter for a parsed profile."""
    return _POLICIES[profile.provider.kind]


def _parsed_profile(profile: ProvisioningProfile | Mapping[str, object]) -> ProvisioningProfile:
    if isinstance(profile, ProvisioningProfile):
        return profile
    return ProvisioningProfile.parse(profile)


def rootfs_source(profile: ProvisioningProfile) -> RootfsSource | None:
    """Return the profile's rootfs source, or ``None`` for providers that do not use one."""
    return policy_for_profile(profile).rootfs_source(profile)


def rootfs_upload_window_allowed(profile: ProvisioningProfile) -> bool:
    """Return whether the profile's rootfs expects a System upload window."""
    rootfs = rootfs_source(profile)
    return rootfs is not None and rootfs.kind == "upload"


def reject_rootfs_upload_without_window(profile: ProvisioningProfile) -> None:
    """Reject a profile whose rootfs needs a System upload window in a no-window lane."""
    if rootfs_upload_window_allowed(profile):
        raise CategorizedError(
            "upload-kind rootfs requires systems.define upload window",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def ssh_credential_ref(profile: ProvisioningProfile) -> str | None:
    """Return the SSH credential reference for providers with credential-backed SSH."""
    return policy_for_profile(profile).ssh_credential_ref(profile)


def drgn_live_requires_credential(profile: ProvisioningProfile) -> bool:
    """Return whether this profile's drgn-live transport needs a core-resolved credential."""
    return policy_for_profile(profile).drgn_live_requires_credential(profile)


def validate_profile(profile: ProvisioningProfile) -> None:
    """Reject unsupported provider params and unresolvable rootfs references."""
    policy_for_profile(profile).validate_profile(profile)


def destructive_opt_in(profile: ProvisioningProfile, op: DestructiveJobKind) -> bool:
    """Return whether the profile opts into a destructive operation."""
    return policy_for_profile(profile).destructive_opt_in(profile, op)


def capture_method(profile: ProvisioningProfile | Mapping[str, object]) -> CaptureMethod:
    """Resolve the crash-capture method a provisioning profile enables."""
    parsed = _parsed_profile(profile)
    return policy_for_profile(parsed).capture_method(parsed)
