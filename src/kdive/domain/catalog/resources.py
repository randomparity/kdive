"""Resource domain vocabulary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from kdive.domain._records import DomainModel
from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.ownership import ManagedBy
from kdive.domain.catalog.resource_capabilities import ResourceCapabilities


class ResourceKind(StrEnum):
    """The provider resource kinds."""

    LOCAL_LIBVIRT = "local-libvirt"
    FAULT_INJECT = "fault-inject"
    REMOTE_LIBVIRT = "remote-libvirt"


class Resource(DomainModel):
    """A registered provider resource host."""

    kind: ResourceKind
    capabilities: dict[str, Any] = Field(default_factory=dict)
    pool: str
    cost_class: str
    status: ResourceStatus
    host_uri: str
    cordoned: bool = False
    managed_by: ManagedBy = ManagedBy.RUNTIME
    name: str | None = None
    owner_project: str | None = None
    affinity_allowlist: list[str] = Field(default_factory=list)
    lease_expires_at: datetime | None = None

    @property
    def capability_view(self) -> ResourceCapabilities:
        return ResourceCapabilities.from_mapping(self.capabilities)


__all__ = ["ManagedBy", "Resource", "ResourceKind"]
