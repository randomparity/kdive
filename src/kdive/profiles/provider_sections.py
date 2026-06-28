"""The provider-section registry (ADR-0269): the single source mapping each
``ResourceKind`` to its provisioning-profile section model, alias, and label.

The agent-facing schema projection, the call-time guard, and ``profile_examples``
all iterate this registry, so a new provider is covered by one entry here plus its
``ResourceKind`` member, its section model + the static ``ProviderSection`` field, and a
composition opt-in — never by editing each agent surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from kdive.domain.catalog.resources import ResourceKind
from kdive.profiles.provisioning import (
    FaultInjectProfile,
    LibvirtProfile,
    RemoteLibvirtProfile,
)


@dataclass(frozen=True, slots=True)
class ProviderSectionSpec:
    """One provider's agent-facing provisioning metadata."""

    kind: ResourceKind
    alias: str
    model: type[BaseModel]
    label: str


PROVIDER_SECTIONS: dict[ResourceKind, ProviderSectionSpec] = {
    ResourceKind.LOCAL_LIBVIRT: ProviderSectionSpec(
        ResourceKind.LOCAL_LIBVIRT,
        ResourceKind.LOCAL_LIBVIRT.value,
        LibvirtProfile,
        "local-libvirt (direct-kernel)",
    ),
    ResourceKind.REMOTE_LIBVIRT: ProviderSectionSpec(
        ResourceKind.REMOTE_LIBVIRT,
        ResourceKind.REMOTE_LIBVIRT.value,
        RemoteLibvirtProfile,
        "remote-libvirt (disk-image)",
    ),
    ResourceKind.FAULT_INJECT: ProviderSectionSpec(
        ResourceKind.FAULT_INJECT,
        ResourceKind.FAULT_INJECT.value,
        FaultInjectProfile,
        "fault-inject (test/mock fixture)",
    ),
}


def aliases_for(kinds: frozenset[ResourceKind]) -> frozenset[str]:
    """Return the profile-section aliases for the live ``kinds`` (ADR-0269)."""
    return frozenset(PROVIDER_SECTIONS[k].alias for k in kinds if k in PROVIDER_SECTIONS)
