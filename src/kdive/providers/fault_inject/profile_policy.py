"""Fault-inject provisioning-profile policy adapter."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.domain.operations.jobs import JobKind
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource


class FaultInjectProfilePolicy:
    """Behavior decisions owned by the fault-inject profile section."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource | None:
        return None

    def drgn_live_seeds_bootstrap_key(self, profile: ProvisioningProfile) -> bool:
        return False

    def validate_profile(self, profile: ProvisioningProfile) -> None:
        return None

    def destructive_opt_in(self, profile: ProvisioningProfile, op: JobKind) -> bool:
        return op.value in profile.provider.fault_inject.destructive_ops

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        return profile.provider.fault_inject.capture_method

    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool:
        return False

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool:
        return False
