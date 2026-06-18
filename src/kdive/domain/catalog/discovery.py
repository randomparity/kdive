"""Core discovery records shared by providers and resource registration services."""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.resources import ResourceKind


class ResourceRecord(TypedDict):
    """A discovered resource host."""

    resource_id: str
    kind: ResourceKind
    capabilities: dict[str, Any]
    status: ResourceStatus


class DiscoverySource(Protocol):
    """Source of discovered resources for core registration."""

    def list_resources(self) -> list[ResourceRecord]: ...
