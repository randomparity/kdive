"""Resource capability value-object tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from kdive.domain.catalog.resource_capabilities import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    DISK_GB_KEY,
    GUEST_ARCHES_KEY,
    MEMORY_MB_KEY,
    PCIE_DEVICES_KEY,
    VCPUS_KEY,
    GuestArch,
    ResourceCapabilities,
    resolve_accel_emulator,
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


def test_resource_capabilities_accepts_zero_size_ceiling() -> None:
    caps = ResourceCapabilities.from_mapping({VCPUS_KEY: 0, MEMORY_MB_KEY: 0})

    assert caps.size_ceiling() == (0, 0)
    assert caps.require_size_ceiling(resource_id=uuid4(), resource_name=None) == (0, 0)


def test_resource_capabilities_accepts_zero_allocation_cap() -> None:
    caps = ResourceCapabilities.from_mapping({CONCURRENT_ALLOCATION_CAP_KEY: 0})

    assert caps.allocation_cap() == 0
    assert caps.require_allocation_cap(resource_id=uuid4()) == 0


def test_disk_ceiling_reads_non_negative_int() -> None:
    caps = ResourceCapabilities.from_mapping({DISK_GB_KEY: 100})

    assert caps.disk_ceiling() == 100


@pytest.mark.parametrize("bad", [None, "100", -1, True])
def test_disk_ceiling_none_when_absent_or_invalid(bad: object) -> None:
    # An unadvertised or invalid ceiling reads as None (unbounded — the provider does not
    # size a disk from host storage); admission skips the bound rather than failing closed.
    caps = ResourceCapabilities.from_mapping({DISK_GB_KEY: bad})

    assert caps.disk_ceiling() is None


def test_disk_ceiling_accepts_zero() -> None:
    caps = ResourceCapabilities.from_mapping({DISK_GB_KEY: 0})

    assert caps.disk_ceiling() == 0


def test_disk_gb_key_not_in_extras() -> None:
    caps = ResourceCapabilities.from_mapping({DISK_GB_KEY: 40, "other": 1})

    assert DISK_GB_KEY not in caps.extras()
    assert caps.extras() == {"other": 1}


def test_require_size_ceiling_reports_memory_when_only_memory_invalid() -> None:
    caps = ResourceCapabilities.from_mapping({VCPUS_KEY: 8, MEMORY_MB_KEY: None})

    with pytest.raises(CategorizedError) as exc:
        caps.require_size_ceiling(resource_id=uuid4(), resource_name="host-a")

    assert exc.value.details["key"] == MEMORY_MB_KEY
    assert MEMORY_MB_KEY in str(exc.value)


def test_require_size_ceiling_reports_vcpus_when_only_vcpus_invalid() -> None:
    caps = ResourceCapabilities.from_mapping({VCPUS_KEY: -1, MEMORY_MB_KEY: 4096})

    with pytest.raises(CategorizedError) as exc:
        caps.require_size_ceiling(resource_id=uuid4(), resource_name="host-b")

    assert exc.value.details["key"] == VCPUS_KEY


def test_guest_arches_reads_well_formed_mapping() -> None:
    caps = ResourceCapabilities.from_mapping(
        {
            GUEST_ARCHES_KEY: {
                "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"},
                "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"},
            }
        }
    )

    assert caps.guest_arches() == {
        "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"},
        "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"},
    }


def test_guest_arches_empty_when_absent() -> None:
    assert ResourceCapabilities.from_mapping({VCPUS_KEY: 8}).guest_arches() == {}


@pytest.mark.parametrize("bad", [None, "x86_64", [], 3])
def test_guest_arches_empty_when_value_is_not_a_mapping(bad: object) -> None:
    assert ResourceCapabilities.from_mapping({GUEST_ARCHES_KEY: bad}).guest_arches() == {}


def test_guest_arches_drops_malformed_entries() -> None:
    # A stale or hand-edited row must never crash a consumer: only entries that are a dict with
    # string accel/emulator survive; the extra key is dropped from the returned GuestArch.
    caps = ResourceCapabilities.from_mapping(
        {
            GUEST_ARCHES_KEY: {
                "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64", "x": 1},
                "ppc64le": "not-a-dict",
                "s390x": {"accel": 7, "emulator": "/usr/bin/qemu-system-s390x"},
                "riscv64": {"emulator": "/usr/bin/qemu-system-riscv64"},
            }
        }
    )

    assert caps.guest_arches() == {
        "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"}
    }


def test_guest_arches_key_not_in_extras() -> None:
    caps = ResourceCapabilities.from_mapping({GUEST_ARCHES_KEY: {}, "other": 1})

    assert GUEST_ARCHES_KEY not in caps.extras()
    assert caps.extras() == {"other": 1}


_X86_KVM: dict[str, GuestArch] = {
    "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"},
    "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"},
}


def test_resolve_accel_emulator_returns_pair_for_advertised_arch() -> None:
    assert resolve_accel_emulator(_X86_KVM, "x86_64") == (
        "kvm",
        "/usr/bin/qemu-system-x86_64",
    )
    assert resolve_accel_emulator(_X86_KVM, "ppc64le") == (
        "tcg",
        "/usr/bin/qemu-system-ppc64",
    )


def test_resolve_accel_emulator_fails_open_on_empty_map() -> None:
    # Empty map -> None so the caller can substitute its legacy default (matches ADR-0339).
    assert resolve_accel_emulator({}, "x86_64") is None


def test_guest_arches_round_trips_parser_output_unchanged() -> None:
    # #1140 follow-up guard: a full-shape parse_guest_arches output must survive the reader
    # unchanged. #1142 deliberately does NOT extend GuestArch (domain type is derived from
    # accel, not stored), so parser and reader stay in sync; if a future field is added to one
    # side but not the other, this fails rather than silently dropping it.
    from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
    from kdive.providers.shared.libvirt_xml import parse_guest_arches

    caps_xml = (
        "<capabilities><host><cpu><arch>x86_64</arch></cpu></host>"
        "<guest><os_type>hvm</os_type><arch name='x86_64'>"
        "<emulator>/usr/bin/qemu-system-x86_64</emulator>"
        "<domain type='qemu'/><domain type='kvm'/></arch></guest>"
        "<guest><os_type>hvm</os_type><arch name='ppc64le'>"
        "<emulator>/usr/bin/qemu-system-ppc64</emulator>"
        "<domain type='qemu'/></arch></guest></capabilities>"
    )
    parsed = parse_guest_arches(caps_xml, SUPPORTED_ARCHES)
    read_back = ResourceCapabilities.from_mapping({GUEST_ARCHES_KEY: parsed}).guest_arches()
    assert read_back == parsed


def test_resolve_accel_emulator_fails_closed_naming_supported_set() -> None:
    ppc_only: dict[str, GuestArch] = {
        "ppc64le": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-ppc64"}
    }

    with pytest.raises(CategorizedError) as exc_info:
        resolve_accel_emulator(ppc_only, "x86_64")

    exc = exc_info.value
    assert exc.category is ErrorCategory.CONFIGURATION_ERROR
    assert "x86_64" in str(exc)
    assert "ppc64le" in str(exc)  # the supported set is named in the message
    assert exc.details == {"requested_arch": "x86_64", "accepted_values": ["ppc64le"]}


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
