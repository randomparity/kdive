"""Provider-neutral helpers for parsed provisioning-profile policy decisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource


class ProfilePolicy(Protocol):
    """Provider-owned behavior derived from a parsed provisioning profile."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource | None: ...

    def drgn_live_seeds_bootstrap_key(self, profile: ProvisioningProfile) -> bool:
        """Return whether a drgn-live ``start_session`` gates+seeds on the per-System bootstrap key.

        True iff the drgn-live transport-open at ``start_session`` authenticates over the loopback
        SSH forward, so the handler must fail closed on a missing per-System bootstrap key and seed
        redaction from it before opening the transport (ADR-0289, ADR-0315). A provider that opens
        drgn-live over the guest agent returns ``False`` — it needs no start-time seed even though
        it still uses the bootstrap key at ``introspect.run``.
        """

    def validate_profile(self, profile: ProvisioningProfile) -> None: ...

    def destructive_opt_in(self, profile: ProvisioningProfile, op: JobKind) -> bool: ...

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod: ...

    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool: ...

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool: ...

    def fadump_provisioned(self, profile: ProvisioningProfile) -> bool:
        """Return whether the System is provisioned for firmware-assisted dump (ADR-0349).

        Only local-libvirt on ppc64le offers fadump; other providers return ``False``. Admission
        gates a fadump-opted provision against the host's discovered ``pseries_fadump`` capability.
        """


def _parsed_profile(profile: ProvisioningProfile | Mapping[str, object]) -> ProvisioningProfile:
    if isinstance(profile, ProvisioningProfile):
        return profile
    return ProvisioningProfile.parse(profile)


def require_investigation_binding_for_upload(
    policy: ProfilePolicy, profile: ProvisioningProfile, investigation_id: UUID | None
) -> None:
    """Require a bound investigation when the profile's rootfs is an ``upload`` (ADR-0441 §2).

    An investigation-scoped uploaded rootfs is resolved by content checksum within the System's
    own investigation, so a ``{"kind": "upload"}`` rootfs with no ``investigation_id`` is
    unresolvable. Reject it at admission with an actionable ``configuration_error`` naming the
    missing binding rather than letting it fail late at provision.

    Args:
        policy: The provider profile policy (to read the rootfs source).
        profile: The parsed provisioning profile.
        investigation_id: The System's effective investigation binding, or ``None``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the rootfs is ``upload`` and
            ``investigation_id`` is ``None``.
    """
    rootfs = policy.rootfs_source(profile)
    if rootfs is not None and rootfs.kind == "upload" and investigation_id is None:
        raise CategorizedError(
            "upload-kind rootfs requires a bound investigation_id: pass investigation_id to "
            "systems.define/provision so the uploaded base resolves within that investigation",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def capture_method(
    policy: ProfilePolicy, profile: ProvisioningProfile | Mapping[str, object]
) -> CaptureMethod:
    parsed = _parsed_profile(profile)
    return policy.capture_method(parsed)
