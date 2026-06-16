"""Provider-aware systems profile validation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from kdive.components.references import ROOTFS_COMPONENT
from kdive.components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource, _UploadRootfs
from kdive.providers.core.runtime import ProfilePolicy

type RootfsValidator = Callable[[RootfsSource], None]


def validate_profile_for_provider(
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    capabilities: ComponentSourceCapabilities,
) -> None:
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
