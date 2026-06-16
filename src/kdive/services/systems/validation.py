"""Provider-aware systems profile validation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from kdive.components.references import ROOTFS_COMPONENT
from kdive.components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DESTRUCTIVE_JOB_KINDS
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource, _UploadRootfs
from kdive.providers.core.runtime import ProfilePolicy

type RootfsValidator = Callable[[RootfsSource], None]

_VALID_DESTRUCTIVE_OP_VALUES = frozenset(kind.value for kind in DESTRUCTIVE_JOB_KINDS)


def _reject_unknown_destructive_ops(profile: ProvisioningProfile) -> None:
    """Reject opt-in tokens outside the closed destructive-op set (ADR-0130).

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
