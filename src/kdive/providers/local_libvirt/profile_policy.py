"""Local-libvirt provisioning-profile policy adapter."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.profiles.provisioning import (
    SUPPORTED_DOMAIN_XML_PARAMS,
    ProvisioningProfile,
    RootfsSource,
    validate_rootfs_reference,
)


class LocalLibvirtProfilePolicy:
    """Behavior decisions owned by the local-libvirt profile section."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource:
        return profile.provider.local_libvirt.rootfs

    def drgn_live_seeds_bootstrap_key(self, profile: ProvisioningProfile) -> bool:
        # Local drgn-live opens over the loopback SSH forward (ADR-0039), so start_session must
        # gate+seed on the per-System bootstrap key (ADR-0289, ADR-0315).
        return True

    def validate_profile(self, profile: ProvisioningProfile) -> None:
        section = profile.provider.local_libvirt
        unknown = sorted(set(section.domain_xml_params) - SUPPORTED_DOMAIN_XML_PARAMS)
        if unknown:
            raise CategorizedError(
                f"unsupported domain_xml_params: {', '.join(unknown)}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"unsupported": unknown, "supported": sorted(SUPPORTED_DOMAIN_XML_PARAMS)},
            )
        validate_rootfs_reference(section.rootfs)

    def destructive_opt_in(self, profile: ProvisioningProfile, op: JobKind) -> bool:
        return op.value in profile.provider.local_libvirt.destructive_ops

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        section = profile.provider.local_libvirt
        if section.crashkernel is not None:
            return CaptureMethod.KDUMP
        if section.debug.gdbstub:
            return CaptureMethod.GDBSTUB
        if section.debug.preserve_on_crash:
            return CaptureMethod.HOST_DUMP
        return CaptureMethod.CONSOLE

    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool:
        return profile.provider.local_libvirt.debug.gdbstub

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool:
        return profile.provider.local_libvirt.debug.preserve_on_crash
