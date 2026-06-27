"""Tests for shared provider port value types."""

from __future__ import annotations

from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.discovery import ResourceRecord
from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.ports.handles import (
    SystemHandle,
    TransportHandle,
)


def test_shared_provider_handles_are_distinct_types() -> None:
    system = SystemHandle("system-1")
    transport = TransportHandle("transport-1")

    assert system == "system-1"
    assert transport == "transport-1"


def test_discovery_records_keep_resource_shape() -> None:
    record: ResourceRecord = {
        "resource_id": "host-1",
        "kind": ResourceKind.LOCAL_LIBVIRT,
        "capabilities": {"arch": "x86_64"},
        "status": ResourceStatus.AVAILABLE,
    }

    assert record["resource_id"] == "host-1"
    assert record["kind"] is ResourceKind.LOCAL_LIBVIRT
    assert record["status"] is ResourceStatus.AVAILABLE
