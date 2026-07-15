"""Remote-libvirt provisioning-profile policy adapter."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.domain.operations.jobs import JobKind
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource


class RemoteLibvirtProfilePolicy:
    """Behavior decisions owned by the remote-libvirt profile section."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource | None:
        return None

    def drgn_live_seeds_bootstrap_key(self, profile: ProvisioningProfile) -> bool:
        # Remote has and uses a per-System bootstrap key at introspect.run, but opens its drgn-live
        # transport over the guest agent, so start_session needs no bootstrap-key seed (ADR-0315).
        return False

    def validate_profile(self, profile: ProvisioningProfile) -> None:
        return None

    def destructive_opt_in(self, profile: ProvisioningProfile, op: JobKind) -> bool:
        return op.value in profile.provider.remote_libvirt.destructive_ops

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        if profile.provider.remote_libvirt.crashkernel is not None:
            return CaptureMethod.KDUMP
        return CaptureMethod.GDBSTUB

    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool:
        # Remote unconditionally provisions a gdbstub endpoint (ADR-0083).
        return True

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool:
        # The remote section has no preserve-on-crash flag; no host-side dump.
        return False

    def fadump_provisioned(self, profile: ProvisioningProfile) -> bool:
        # fadump is a local-libvirt/pseries opt-in (ADR-0349); remote does not offer it.
        return False
