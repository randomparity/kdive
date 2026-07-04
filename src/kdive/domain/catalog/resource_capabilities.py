"""Typed access to shared Resource capability keys."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from kdive.domain.errors import CategorizedError, ErrorCategory

if TYPE_CHECKING:
    from uuid import UUID

    from kdive.domain.pcie import PCIeDescriptor

CONCURRENT_ALLOCATION_CAP_KEY = "concurrent_allocation_cap"
PCIE_DEVICES_KEY = "pcie_devices"

# Billable size ceilings the discovery provider advertises and admission's ≤ resource-caps
# check reads (ADR-0007 §2). A selector may not exceed these.
VCPUS_KEY = "vcpus"
MEMORY_MB_KEY = "memory_mb"

# The per-request disk ceiling (ADR-0312): the largest disk_gb an allocation may request on
# this host. local-libvirt derives it from host storage at discovery; remote/fault-inject
# declare it in systems.toml. A selector's disk_gb may not exceed it.
DISK_GB_KEY = "disk_gb"

_DESCRIPTOR_FIELDS = ("bdf", "vendor_id", "device_id", "class_code", "label")
_KNOWN_KEYS = frozenset(
    {CONCURRENT_ALLOCATION_CAP_KEY, DISK_GB_KEY, MEMORY_MB_KEY, PCIE_DEVICES_KEY, VCPUS_KEY, "arch"}
)


@dataclass(frozen=True, slots=True)
class ResourceCapabilities:
    """Typed readers over the JSON-compatible Resource capabilities mapping."""

    _values: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> ResourceCapabilities:
        return cls(MappingProxyType(dict(values)))

    def raw(self) -> Mapping[str, Any]:
        return self._values

    def extras(self) -> Mapping[str, Any]:
        extras = {key: value for key, value in self._values.items() if key not in _KNOWN_KEYS}
        return MappingProxyType(extras)

    def scalar(self, key: str) -> Any:
        return self._values.get(key)

    def allocation_cap(self) -> int | None:
        return _non_negative_int(self._values.get(CONCURRENT_ALLOCATION_CAP_KEY))

    def require_allocation_cap(self, *, resource_id: UUID) -> int:
        cap = self.allocation_cap()
        if cap is None:
            raise CategorizedError(
                f"resource {resource_id} has no valid {CONCURRENT_ALLOCATION_CAP_KEY!r}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "resource_id": str(resource_id),
                    "cap": repr(self._values.get(CONCURRENT_ALLOCATION_CAP_KEY)),
                },
            )
        return cap

    def size_ceiling(self) -> tuple[int, int] | None:
        vcpus = _non_negative_int(self._values.get(VCPUS_KEY))
        memory_mb = _non_negative_int(self._values.get(MEMORY_MB_KEY))
        if vcpus is None or memory_mb is None:
            return None
        return vcpus, memory_mb

    def require_size_ceiling(
        self, *, resource_id: UUID, resource_name: str | None
    ) -> tuple[int, int]:
        ceiling = self.size_ceiling()
        if ceiling is None:
            missing_key = _first_invalid_size_key(self._values)
            label = resource_name or str(resource_id)
            raise CategorizedError(
                f"host {label} advertises no {missing_key} size ceiling; this is a "
                "host-registration gap, not a problem with your request. Re-register the host "
                f"with a {missing_key} value (remote-libvirt/fault-inject declare it in "
                "systems.toml or resources.register_*; local-libvirt gets it from discovery).",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "resource_id": str(resource_id),
                    "resource_name": resource_name,
                    "key": missing_key,
                },
            )
        return ceiling

    def disk_ceiling(self) -> int | None:
        """The largest requestable ``disk_gb`` on this host, or ``None`` if unadvertised.

        ``None`` means the provider does not size a disk from host storage (remote-libvirt
        provisions a disk-image; fault-inject is a fake), so a disk request to it is not
        bounded. local-libvirt always advertises this (live-derived at discovery, ADR-0312),
        so a local host is always bounded.
        """
        return _non_negative_int(self._values.get(DISK_GB_KEY))

    def pcie_descriptors(self) -> list[PCIeDescriptor]:
        raw = self._values.get(PCIE_DEVICES_KEY)
        if not isinstance(raw, list):
            return []
        descriptors: list[PCIeDescriptor] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if any(not isinstance(entry.get(field), str) for field in _DESCRIPTOR_FIELDS):
                continue
            descriptors.append(
                cast(
                    "PCIeDescriptor",
                    {
                        "bdf": entry["bdf"],
                        "vendor_id": entry["vendor_id"],
                        "device_id": entry["device_id"],
                        "class_code": entry["class_code"],
                        "label": entry["label"],
                    },
                )
            )
        return descriptors


def _non_negative_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _first_invalid_size_key(values: Mapping[str, Any]) -> str:
    for key in (VCPUS_KEY, MEMORY_MB_KEY):
        if _non_negative_int(values.get(key)) is None:
            return key
    return VCPUS_KEY
