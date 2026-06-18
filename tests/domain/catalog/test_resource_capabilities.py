"""Resource capability value-object tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from kdive.domain.catalog.resource_capabilities import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    MEMORY_MB_KEY,
    PCIE_DEVICES_KEY,
    VCPUS_KEY,
    ResourceCapabilities,
)
from kdive.domain.errors import CategorizedError, ErrorCategory

_DESCRIPTOR = {
    "bdf": "0000:3b:00.0",
    "vendor_id": "8086",
    "device_id": "1572",
    "class_code": "020000",
    "label": "Intel X710",
}


def test_resource_capabilities_read_typed_known_values() -> None:
    caps = ResourceCapabilities.from_mapping(
        {
            CONCURRENT_ALLOCATION_CAP_KEY: 2,
            VCPUS_KEY: 8,
            MEMORY_MB_KEY: 16384,
            PCIE_DEVICES_KEY: [_DESCRIPTOR],
            "provider_specific": {"kept": True},
        }
    )

    assert caps.allocation_cap() == 2
    assert caps.size_ceiling() == (8, 16384)
    assert caps.pcie_descriptors() == [_DESCRIPTOR]
    assert caps.extras()["provider_specific"] == {"kept": True}


@pytest.mark.parametrize("bad", [None, "2", -1, True])
def test_resource_capabilities_reject_invalid_allocation_cap(bad: object) -> None:
    caps = ResourceCapabilities.from_mapping({CONCURRENT_ALLOCATION_CAP_KEY: bad})

    assert caps.allocation_cap() is None
    with pytest.raises(CategorizedError) as exc:
        caps.require_allocation_cap(resource_id=uuid4())

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize("bad", [None, "8", -1, True])
def test_resource_capabilities_reject_invalid_size_ceiling(bad: object) -> None:
    caps = ResourceCapabilities.from_mapping({VCPUS_KEY: bad, MEMORY_MB_KEY: 4096})

    assert caps.size_ceiling() is None
    with pytest.raises(CategorizedError) as exc:
        caps.require_size_ceiling(resource_id=uuid4(), resource_name=None)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resource_capabilities_filters_malformed_pcie_descriptors() -> None:
    caps = ResourceCapabilities.from_mapping(
        {
            PCIE_DEVICES_KEY: [
                _DESCRIPTOR,
                {"bdf": "0000:3b:00.1"},
                "not-a-device",
            ]
        }
    )

    assert caps.pcie_descriptors() == [_DESCRIPTOR]
