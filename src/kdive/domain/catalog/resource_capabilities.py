"""Typed access to shared Resource capability keys."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypedDict, cast

from kdive.domain.errors import CategorizedError, ErrorCategory

if TYPE_CHECKING:
    from uuid import UUID

    from kdive.domain.pcie import PCIeDescriptor

CONCURRENT_ALLOCATION_CAP_KEY = "concurrent_allocation_cap"
PCIE_DEVICES_KEY = "pcie_devices"

# The bootable guest arches a host advertises, each with its accelerator (``kvm``/``tcg``) and
# emulator path (ADR-0338). Populated by local-libvirt discovery from the libvirt capabilities
# ``<guest>`` blocks; admission validates a profile arch against this set.
GUEST_ARCHES_KEY = "guest_arches"

# Whether the host QEMU implements pseries firmware-assisted dump (``ibm,configure-kernel-dump``,
# QEMU ≥10.2) — a fail-closed bool recorded by local-libvirt discovery (ADR-0349). Admission gates
# a fadump-opted provision against it; absent/non-bool reads as ``False`` (never fadump by default).
PSERIES_FADUMP_KEY = "pseries_fadump"


class GuestArch(TypedDict):
    """One bootable guest arch's accelerator and emulator, as advertised by discovery."""

    accel: str
    emulator: str


def resolve_accel_emulator(
    guest_arches: Mapping[str, GuestArch], arch: str
) -> tuple[str, str] | None:
    """Validate ``arch`` against a resource's guest arches; resolve its ``(accel, emulator)``.

    The single branch definition shared by Systems admission (ADR-0339,
    ``services/systems/validation.py:resolve_accel``) and the local-libvirt provisioner
    (ADR-0340), so the two resolution sites cannot drift: admission validated the persisted
    capability_view at mint, the provisioner re-resolves live capabilities at provision, and
    both must fail open / closed identically.

    Args:
        guest_arches: ``{arch: {"accel", "emulator"}}`` as
            :meth:`ResourceCapabilities.guest_arches` returns, filtered to the
            kdive-provisionable set (ADR-0338).
        arch: The profile architecture to resolve.

    Returns:
        ``(accel, emulator)`` for ``arch``, or ``None`` when the resource advertises **no**
        guest arches — remote-libvirt, fault-inject, or a host not re-discovered since
        ADR-0338. That fail-open case lets the caller substitute its own default
        (admission records no accel; the provisioner renders the legacy x86-KVM domain).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when ``guest_arches`` is non-empty and does
            not advertise ``arch``. The message names the supported set — the same fail-fast
            rule as ``arch_traits()``, never a silent x86 fallback.
    """
    if not guest_arches:
        return None
    entry = guest_arches.get(arch)
    if entry is None:
        supported = sorted(guest_arches)
        raise CategorizedError(
            f"resource does not support guest architecture {arch!r}; supported: "
            f"{', '.join(supported)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            # `accepted_values` is the ADR-0224 reserved key that survives `safe_error_details`
            # and reaches the agent as a structured finite set; a custom key's list is dropped.
            details={"requested_arch": arch, "accepted_values": supported},
        )
    return entry["accel"], entry["emulator"]


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
    {
        CONCURRENT_ALLOCATION_CAP_KEY,
        DISK_GB_KEY,
        GUEST_ARCHES_KEY,
        MEMORY_MB_KEY,
        PCIE_DEVICES_KEY,
        PSERIES_FADUMP_KEY,
        VCPUS_KEY,
        "arch",
    }
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

    def guest_arches(self) -> dict[str, GuestArch]:
        """The bootable guest arches this host advertises, dropping malformed entries (ADR-0338).

        Defensive over the persisted JSON (mirrors :meth:`pcie_descriptors`): a stale or
        hand-edited row never crashes a consumer. Returns ``{}`` when the key is absent or is not
        a mapping; keeps only entries that are a dict with string ``accel`` and ``emulator``, and
        returns each as a bare :class:`GuestArch` (extra keys dropped). Does not validate the
        ``accel`` value domain — the renderer (ADR-0340) maps ``kvm`` to a KVM domain and treats
        any other value as TCG (``qemu``), a total mapping that is safe because
        :func:`~kdive.providers.shared.libvirt_xml.parse_guest_arches` only ever emits
        ``kvm``/``tcg``.
        """
        raw = self._values.get(GUEST_ARCHES_KEY)
        if not isinstance(raw, Mapping):
            return {}
        arches: dict[str, GuestArch] = {}
        for arch, entry in raw.items():
            if not isinstance(entry, Mapping):
                continue
            accel = entry.get("accel")
            emulator = entry.get("emulator")
            if isinstance(accel, str) and isinstance(emulator, str):
                arches[arch] = {"accel": accel, "emulator": emulator}
        return arches

    def pseries_fadump(self) -> bool:
        """Whether the host QEMU implements pseries fadump (ADR-0349), fail-closed.

        Returns ``True`` only for a stored ``bool`` ``True``; an absent key, a non-``bool``
        value, or a stale/hand-edited row reads as ``False`` so a fadump-opted provision is never
        admitted against a host that does not advertise support.
        """
        return self._values.get(PSERIES_FADUMP_KEY) is True

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
