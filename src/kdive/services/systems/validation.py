"""Provider-aware systems profile validation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from kdive.components.references import ROOTFS_COMPONENT
from kdive.components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)
from kdive.domain.catalog.resource_capabilities import GuestArch, resolve_accel_emulator
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import OPT_IN_DESTRUCTIVE_JOB_KINDS
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource, _UploadRootfs

type RootfsValidator = Callable[[RootfsSource], None]

# The accepted tokens are exactly the ops whose opt-in factor is resolved from
# ``destructive_ops`` (ADR-0320) — not every destructive job kind. ``power`` (contributor
# lifecycle) and ``teardown`` (role-only gate, ADR-0129) gate nothing via this list, so they
# are rejected as non-gating tokens rather than silently accepted as inert phantom knobs.
_VALID_DESTRUCTIVE_OP_VALUES = frozenset(kind.value for kind in OPT_IN_DESTRUCTIVE_JOB_KINDS)


def _reject_unknown_destructive_ops(profile: ProvisioningProfile) -> None:
    """Reject opt-in tokens outside the opt-in-consuming destructive-op set (ADR-0130, ADR-0320).

    Once profile opt-in is the load-bearing grant, a typo would be a silent permanent denial
    indistinguishable from an intentional empty list. Runs at the write boundary only;
    ``ProvisioningProfile.parse`` stays structural so the unguarded read-path parse in
    ``control._op_opt_in`` cannot raise on a stored legacy token.
    """
    unknown = sorted(
        op for op in profile.provider.destructive_ops if op not in _VALID_DESTRUCTIVE_OP_VALUES
    )
    if unknown:
        raise CategorizedError(
            "provisioning profile declares unknown destructive_ops tokens",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "unknown_destructive_ops": unknown,
                "valid_destructive_ops": sorted(_VALID_DESTRUCTIVE_OP_VALUES),
            },
        )


def resolve_accel(guest_arches: Mapping[str, GuestArch], arch: str) -> str | None:
    """Validate ``arch`` against a resource's guest arches and resolve its accelerator (ADR-0339).

    A thin wrapper over :func:`resolve_accel_emulator` (the one branch definition shared with the
    local-libvirt provisioner, ADR-0340) that keeps admission's accel-only ``str | None``
    contract: the emulator is dropped here and only the provisioner's renderer consumes it.

    ``guest_arches`` is what :meth:`ResourceCapabilities.guest_arches` returns for the bound
    Resource — ``{arch: {"accel", "emulator"}}`` filtered to the kdive-provisionable set (ADR-0338).

    Returns:
        The advertised accelerator name (``kvm``/``tcg``) for ``arch``, or ``None`` when the
        resource advertises **no** guest arches — remote-libvirt, fault-inject, or a host not
        re-discovered since ADR-0338. That fail-open case skips the check and records no accel,
        preserving pre-ADR-0339 behavior.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when ``guest_arches`` is non-empty and does not
            advertise ``arch``. The message names the supported set — the same fail-fast rule as
            ``arch_traits()``, never a silent x86 fallback.
    """
    resolved = resolve_accel_emulator(guest_arches, arch)
    return resolved[0] if resolved is not None else None


def validate_profile_for_provider(
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    capabilities: ComponentSourceCapabilities,
) -> None:
    _reject_unknown_destructive_ops(profile)
    profile_policy.validate_profile(profile)
    rootfs = profile_policy.rootfs_source(profile)
    if rootfs is None:
        return
    if isinstance(rootfs, _UploadRootfs):
        return
    reject_unsupported_component_source(
        capabilities,
        component_kind=ROOTFS_COMPONENT,
        ref=rootfs,
    )


async def validate_rootfs_for_provider(
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
) -> None:
    """Run the synchronous provider rootfs validator off the event loop (ADR-0126).

    The validator can do blocking disk/network I/O (the ``local-libvirt`` validator
    materializes a rootfs base), so it is offloaded to a worker thread; one provision
    request can no longer stall the asyncio event loop for unrelated concurrent requests.
    The ``None``/upload early returns do no I/O and stay synchronous.
    """
    rootfs = profile_policy.rootfs_source(profile)
    if rootfs is None:
        return
    if isinstance(rootfs, _UploadRootfs):
        return
    await asyncio.to_thread(rootfs_validator, rootfs)
