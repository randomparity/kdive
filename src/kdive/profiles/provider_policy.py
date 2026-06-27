"""Provider-neutral helpers for parsed provisioning-profile policy decisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource


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

    def destructive_opt_in(self, profile: ProvisioningProfile, op: JobKind) -> bool:
        """Return whether the profile opts into a destructive operation."""

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        """Resolve the crash-capture method enabled by the profile."""

    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool:
        """Return whether the System has a gdbstub endpoint independent of capture method."""

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool:
        """Return whether a host-side memory dump is available on a preserved crash."""


def _parsed_profile(profile: ProvisioningProfile | Mapping[str, object]) -> ProvisioningProfile:
    if isinstance(profile, ProvisioningProfile):
        return profile
    return ProvisioningProfile.parse(profile)


def rootfs_upload_window_allowed(policy: ProfilePolicy, profile: ProvisioningProfile) -> bool:
    """Return whether the profile's rootfs expects a System upload window."""
    rootfs = policy.rootfs_source(profile)
    return rootfs is not None and rootfs.kind == "upload"


def reject_rootfs_upload_without_window(
    policy: ProfilePolicy, profile: ProvisioningProfile
) -> None:
    """Reject a profile whose rootfs needs a System upload window in a no-window lane."""
    if rootfs_upload_window_allowed(policy, profile):
        raise CategorizedError(
            "upload-kind rootfs requires systems.define upload window",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def capture_method(
    policy: ProfilePolicy, profile: ProvisioningProfile | Mapping[str, object]
) -> CaptureMethod:
    """Resolve the crash-capture method a provisioning profile enables."""
    parsed = _parsed_profile(profile)
    return policy.capture_method(parsed)
