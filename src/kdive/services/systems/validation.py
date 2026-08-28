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
    """Resolve the advertised accelerator while preserving admission's accel-only contract."""
    resolved = resolve_accel_emulator(guest_arches, arch)
    return resolved[0] if resolved is not None else None


def require_fadump_supported(*, requested: bool, supported: bool) -> None:
    """Fail closed when a requested fadump host does not advertise support (ADR-0349)."""
    if not requested or supported:
        return
    raise CategorizedError(
        "the bound host does not implement pseries fadump; it needs QEMU >= 10.2 "
        "(the ibm,configure-kernel-dump RTAS). Re-run resource discovery if you recently "
        "upgraded QEMU, provision on a fadump-capable host, or drop debug.fadump for kdump.",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": "pseries_fadump_unsupported", "qemu_floor": "10.2"},
    )


def _require_profile_matches_resource_kind(
    profile: ProvisioningProfile, profile_policy: ProfilePolicy
) -> None:
    """Reject a provider section that differs from the bound Resource kind (ADR-0549)."""
    declared = profile.provider.kind
    if declared is profile_policy.kind:
        return
    raise CategorizedError(
        f"provisioning profile declares a {declared.value!r} provider section, but the bound "
        f"Resource is kind {profile_policy.kind.value!r}; supply a "
        f"{profile_policy.kind.value!r} provider section (for a new System, request an allocation "
        f"on a {declared.value!r} resource instead)",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "profile_provider_section": declared.value,
            "resource_kind": profile_policy.kind.value,
        },
    )


def validate_profile_for_provider(
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    capabilities: ComponentSourceCapabilities,
) -> None:
    _require_profile_matches_resource_kind(profile, profile_policy)
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
