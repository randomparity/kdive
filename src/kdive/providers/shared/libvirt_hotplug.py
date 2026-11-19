"""Libvirt device hot-plug shared by the local and remote providers (ADR-0823).

The domain-side logic is the same on both hosts: build one device's XML, ``attachDeviceFlags`` /
``detachDeviceFlags`` it with the live flag only, find it again by its user alias, and map libvirt
faults to the provider taxonomy. Only the connection (a local ``libvirt.open`` or the remote
mutual-TLS transport), the backing-volume store (a qcow2 file or a storage-pool volume) and the
default NIC source differ, so each provider injects those three seams into
:class:`LibvirtDeviceHotplug` and owns no domain logic of its own.

A device is named by the user alias ``ua-kdive-<device_id>``: libvirt keeps the alias in the live
domain XML, so a device is found by ``device_id`` with no ledger and no persisted definition. NIC
MACs and disk serials derive from ``device_id`` so the identity the ledger records is the one the
guest observes (``ip link``, ``lsblk``). Devices are live-only; a power cycle drops them.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from uuid import UUID

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports._common import config_error
from kdive.providers.ports.lifecycle import DeviceSpec, HotplugDeviceKind, PlugOutcome

_log = logging.getLogger(__name__)

ALIAS_PREFIX: Final = "ua-kdive-"
_BYTES_PER_GB: Final = 1024**3
# virtio disk serials are at most 20 characters.
_SERIAL_CHARS: Final = 20
_DISK_LETTERS: Final = "bcdefghijklmnopqrstuvwxyz"
_LIBVIRT_MESSAGE_CHARS: Final = 300
_ATTACH_FLAGS: Final = libvirt.VIR_DOMAIN_AFFECT_LIVE
_NETWORK_NAME_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")

type NicSource = tuple[Literal["user", "network"], str | None]


class _Domain(Protocol):
    def isActive(self) -> int: ...  # noqa: N802 - libvirt binding name
    def XMLDesc(self, flags: int = 0, /) -> str: ...  # noqa: N802 - libvirt binding name
    def attachDeviceFlags(self, xml: str, flags: int, /) -> int: ...  # noqa: N802
    def detachDeviceFlags(self, xml: str, flags: int, /) -> int: ...  # noqa: N802


class HotplugConnection(Protocol):
    """The connection slice hot-plug uses; storage lookups ride the same connection remotely."""

    def lookupByName(self, name: str, /) -> Any: ...  # noqa: N802 - libvirt binding name


class DeviceVolumes(Protocol):
    """Backing-volume seam for ``disk`` devices; every method is idempotent."""

    def create(
        self, conn: Any, domain_name: str, device_id: str, size_gb: int, /
    ) -> str:  # pragma: no cover - protocol
        """Create a fresh empty qcow2 volume (replacing a stale one) and return its host path."""
        ...

    def remove(
        self, conn: Any, domain_name: str, device_id: str, /
    ) -> None:  # pragma: no cover - protocol
        """Delete the device's volume; an absent volume is the achieved post-state."""
        ...

    def remove_all(self, conn: Any, domain_name: str, /) -> None:  # pragma: no cover - protocol
        """Delete every device volume of the domain."""
        ...


def hotplug_error(
    message: str, category: ErrorCategory, reason: str, **details: object
) -> CategorizedError:
    """A provider error carrying the ADR-0823 ``data.reason``."""
    return CategorizedError(message, category=category, details={"reason": reason, **details})


def parse_device_id(raw: str) -> str:
    """Canonical lowercase UUID text of ``raw``; the id is embedded in a name, a path and XML.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when ``raw`` is not a UUID.
    """
    try:
        return str(UUID(raw))
    except ValueError:
        raise config_error("device_id must be a UUID") from None


def device_alias(device_id: str) -> str:
    """The libvirt user alias of a kdive device."""
    return f"{ALIAS_PREFIX}{device_id}"


def _digest(device_id: str) -> bytes:
    return hashlib.blake2b(device_id.encode(), digest_size=8).digest()


def nic_mac(device_id: str) -> str:
    """The deterministic locally administered QEMU-OUI MAC of a NIC device."""
    return "52:54:00:" + ":".join(f"{byte:02x}" for byte in _digest(device_id)[:3])


def disk_serial(device_id: str) -> str:
    """The deterministic virtio serial of a disk device (what ``lsblk -o SERIAL`` shows)."""
    return f"kdive-{_digest(device_id).hex()}"[:_SERIAL_CHARS]


def render_nic_xml(device_id: str, source: NicSource) -> str:
    """``<interface>`` XML of a virtio NIC, user-mode or on a libvirt network."""
    kind, network = source
    interface = ET.Element("interface", type=kind)
    ET.SubElement(interface, "mac", address=nic_mac(device_id))
    if kind == "network":
        ET.SubElement(interface, "source", network=str(network))
    ET.SubElement(interface, "model", type="virtio")
    ET.SubElement(interface, "alias", name=device_alias(device_id))
    return ET.tostring(interface, encoding="unicode")


def render_disk_xml(device_id: str, *, path: str, target: str) -> str:
    """``<disk>`` XML of a virtio-blk qcow2 scratch disk at ``path``."""
    disk = ET.Element("disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2", discard="unmap")
    ET.SubElement(disk, "source", file=path)
    ET.SubElement(disk, "target", dev=target, bus="virtio")
    ET.SubElement(disk, "serial").text = disk_serial(device_id)
    ET.SubElement(disk, "alias", name=device_alias(device_id))
    return ET.tostring(disk, encoding="unicode")


@dataclass(frozen=True, slots=True)
class LiveDevice:
    """One kdive device found in a domain's live XML."""

    device_id: str
    kind: HotplugDeviceKind
    identity: dict[str, str]
    xml: str


def _devices_root(domain_xml: str, *, domain_name: str) -> ET.Element:
    try:
        root = _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise hotplug_error(
            "libvirt returned unparseable domain XML",
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            "domain_xml_invalid",
            domain=domain_name,
        ) from exc
    devices = root.find("devices")
    return devices if devices is not None else ET.Element("devices")


def _live_device(element: ET.Element) -> LiveDevice | None:
    alias = element.find("alias")
    name = alias.get("name", "") if alias is not None else ""
    if not name.startswith(ALIAS_PREFIX):
        return None
    device_id = name.removeprefix(ALIAS_PREFIX)
    xml = ET.tostring(element, encoding="unicode")
    if element.tag == "interface":
        mac = element.find("mac")
        identity = {"mac": mac.get("address", "")} if mac is not None else {}
        return LiveDevice(device_id, HotplugDeviceKind.NIC, identity, xml)
    serial = element.findtext("serial", default="")
    return LiveDevice(device_id, HotplugDeviceKind.DISK, {"serial": serial}, xml)


def live_devices(domain_xml: str, *, domain_name: str) -> list[LiveDevice]:
    """Every kdive-plugged NIC and disk in the domain's live XML."""
    devices = _devices_root(domain_xml, domain_name=domain_name)
    elements = [*devices.findall("interface"), *devices.findall("disk")]
    return [device for device in map(_live_device, elements) if device is not None]


def free_disk_target(domain_xml: str, *, domain_name: str) -> str:
    """The first unused ``vd[b-z]`` target name.

    Raises:
        CategorizedError: ``CONTROL_FAILURE`` (``no_free_target``) when all are taken.
    """
    devices = _devices_root(domain_xml, domain_name=domain_name)
    used = {t.get("dev", "") for t in devices.findall("disk/target")}
    for letter in _DISK_LETTERS:
        if f"vd{letter}" not in used:
            return f"vd{letter}"
    raise hotplug_error(
        "the domain has no free virtio disk name",
        ErrorCategory.CONTROL_FAILURE,
        "no_free_target",
        domain=domain_name,
    )


def _libvirt_text(exc: libvirt.libvirtError) -> str:
    return (exc.get_error_message() or "")[:_LIBVIRT_MESSAGE_CHARS]


def libvirt_failure(exc: libvirt.libvirtError, verb: str, domain_name: str) -> CategorizedError:
    """Map a libvirt fault on ``verb`` to the provider taxonomy (ADR-0823 errors)."""
    code = exc.get_error_code()
    details: dict[str, object] = {"domain": domain_name, "libvirt_error": _libvirt_text(exc)}
    if code == libvirt.VIR_ERR_NO_DOMAIN:
        return hotplug_error(
            f"domain {domain_name} is not defined",
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            "domain_absent",
            **details,
        )
    if code == libvirt.VIR_ERR_NO_NETWORK:
        return hotplug_error(
            "the requested network does not exist on the host",
            ErrorCategory.CONFIGURATION_ERROR,
            "unknown_network",
            **details,
        )
    if code == libvirt.VIR_ERR_OPERATION_INVALID:
        return hotplug_error(
            f"domain {domain_name} is not running",
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            "domain_not_running",
            **details,
        )
    return CategorizedError(
        f"libvirt error {verb} domain",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=details,
    )


def _unplug_failure(exc: libvirt.libvirtError, domain_name: str) -> CategorizedError:
    refusal = {libvirt.VIR_ERR_OPERATION_FAILED, libvirt.VIR_ERR_OPERATION_UNSUPPORTED}
    if exc.get_error_code() in refusal:
        return hotplug_error(
            "the guest or hypervisor refused to release the device",
            ErrorCategory.CONTROL_FAILURE,
            "unplug_refused",
            domain=domain_name,
            libvirt_error=_libvirt_text(exc),
        )
    return libvirt_failure(exc, "detaching a device from", domain_name)


def _reject_network(network: str | None) -> None:
    if network is not None and _NETWORK_NAME_RE.fullmatch(network) is None:
        raise config_error("network must be 1..64 characters of [A-Za-z0-9._-]")


class LibvirtDeviceHotplug:
    """The libvirt :class:`~kdive.providers.ports.lifecycle.DeviceHotplugger`.

    ``connection`` opens (and closes) the host connection per call; ``volumes`` backs ``disk``
    devices; ``nic_source`` resolves a request's network name to the interface source.
    """

    def __init__(
        self,
        *,
        connection: Callable[[], AbstractContextManager[HotplugConnection]],
        volumes: DeviceVolumes,
        nic_source: Callable[[str | None], NicSource],
        kinds: frozenset[HotplugDeviceKind],
    ) -> None:
        self._connection = connection
        self._volumes = volumes
        self._nic_source = nic_source
        self._kinds = kinds

    def plug(self, domain_name: str, spec: DeviceSpec) -> PlugOutcome:
        """Attach a NIC or scratch disk to the running domain; idempotent on the device id."""
        if spec.kind not in self._kinds:
            raise config_error(
                f"device kind {spec.kind!r} is not supported; supported: {sorted(self._kinds)}"
            )
        device_id = parse_device_id(spec.device_id)
        _reject_network(spec.network)
        size_gb = self._disk_size(spec)
        with self._connection() as conn:
            domain = _lookup(conn, domain_name)
            self._require_running(domain, domain_name)
            domain_xml = _domain_xml(domain, domain_name)
            existing = _find(domain_xml, device_id, domain_name)
            if existing is not None:
                return _outcome(existing)
            if spec.kind is HotplugDeviceKind.NIC:
                device_xml = render_nic_xml(device_id, self._nic_source(spec.network))
                self._attach(domain, domain_name, device_xml)
            else:
                self._plug_disk(conn, domain, domain_name, device_id, size_gb, domain_xml)
            return _outcome(self._require_live(domain, domain_name, device_id))

    def unplug(self, domain_name: str, device_id: str, provider_handle: str | None) -> bool:
        """Send the graceful detach; ``True`` when the device is already gone."""
        device_id = parse_device_id(device_id)
        self._check_handle(device_id, provider_handle)
        with self._connection() as conn:
            domain = _lookup(conn, domain_name)
            live = _find(_domain_xml(domain, domain_name), device_id, domain_name)
            if live is None:
                self._volumes.remove(conn, domain_name, device_id)
                return True
            self._require_running(domain, domain_name)
            try:
                domain.detachDeviceFlags(live.xml, _ATTACH_FLAGS)
            except libvirt.libvirtError as exc:
                raise _unplug_failure(exc, domain_name) from exc
            # libvirt waits a few seconds for the guest; a prompt guest is already done.
            if _find(_domain_xml(domain, domain_name), device_id, domain_name) is None:
                self._volumes.remove(conn, domain_name, device_id)
                return True
            return False

    def is_attached(self, domain_name: str, device_id: str, provider_handle: str | None) -> bool:
        """Whether the device is in the live domain; the first absent look frees its volume."""
        device_id = parse_device_id(device_id)
        self._check_handle(device_id, provider_handle)
        with self._connection() as conn:
            domain = _lookup(conn, domain_name)
            if _find(_domain_xml(domain, domain_name), device_id, domain_name) is not None:
                return True
            self._volumes.remove(conn, domain_name, device_id)
            return False

    def release_all(self, domain_name: str) -> None:
        """Delete every device volume of the domain; teardown destroys the domain's devices."""
        with self._connection() as conn:
            self._volumes.remove_all(conn, domain_name)

    @staticmethod
    def _disk_size(spec: DeviceSpec) -> int:
        if spec.kind is not HotplugDeviceKind.DISK:
            return 0
        if spec.size_gb is None or spec.size_gb < 1:
            raise config_error("a disk device needs size_gb >= 1")
        return spec.size_gb

    @staticmethod
    def _check_handle(device_id: str, provider_handle: str | None) -> None:
        if provider_handle is not None and provider_handle != device_alias(device_id):
            raise hotplug_error(
                f"no device {device_id} matches the supplied handle",
                ErrorCategory.CONTROL_FAILURE,
                "handle_unknown",
            )

    @staticmethod
    def _require_running(domain: _Domain, domain_name: str) -> None:
        try:
            active = domain.isActive()
        except libvirt.libvirtError as exc:
            raise libvirt_failure(exc, "checking", domain_name) from exc
        if not active:
            raise hotplug_error(
                f"domain {domain_name} is not running",
                ErrorCategory.INFRASTRUCTURE_FAILURE,
                "domain_not_running",
                domain=domain_name,
            )

    @staticmethod
    def _attach(domain: _Domain, domain_name: str, device_xml: str) -> None:
        try:
            domain.attachDeviceFlags(device_xml, _ATTACH_FLAGS)
        except libvirt.libvirtError as exc:
            raise libvirt_failure(exc, "attaching a device to", domain_name) from exc

    def _plug_disk(
        self,
        conn: HotplugConnection,
        domain: _Domain,
        domain_name: str,
        device_id: str,
        size_gb: int,
        domain_xml: str,
    ) -> None:
        target = free_disk_target(domain_xml, domain_name=domain_name)
        path = self._volumes.create(conn, domain_name, device_id, size_gb)
        try:
            self._attach(domain, domain_name, render_disk_xml(device_id, path=path, target=target))
        except CategorizedError:
            with contextlib.suppress(CategorizedError):
                self._volumes.remove(conn, domain_name, device_id)
            raise

    @staticmethod
    def _require_live(domain: _Domain, domain_name: str, device_id: str) -> LiveDevice:
        live = _find(_domain_xml(domain, domain_name), device_id, domain_name)
        if live is None:
            raise hotplug_error(
                "libvirt accepted the device but the live domain does not list it",
                ErrorCategory.INFRASTRUCTURE_FAILURE,
                "device_not_listed",
                domain=domain_name,
            )
        return live


def _lookup(conn: HotplugConnection, domain_name: str) -> _Domain:
    try:
        return conn.lookupByName(domain_name)
    except libvirt.libvirtError as exc:
        raise libvirt_failure(exc, "looking up", domain_name) from exc


def _domain_xml(domain: _Domain, domain_name: str) -> str:
    try:
        return domain.XMLDesc(0)
    except libvirt.libvirtError as exc:
        raise libvirt_failure(exc, "reading", domain_name) from exc


def _find(domain_xml: str, device_id: str, domain_name: str) -> LiveDevice | None:
    for device in live_devices(domain_xml, domain_name=domain_name):
        if device.device_id == device_id:
            return device
    return None


def _outcome(device: LiveDevice) -> PlugOutcome:
    return PlugOutcome(device_alias(device.device_id), dict(device.identity), settled=True)


__all__ = [
    "ALIAS_PREFIX",
    "DeviceVolumes",
    "HotplugConnection",
    "LibvirtDeviceHotplug",
    "LiveDevice",
    "NicSource",
    "device_alias",
    "disk_serial",
    "free_disk_target",
    "hotplug_error",
    "live_devices",
    "nic_mac",
    "parse_device_id",
    "render_disk_xml",
    "render_nic_xml",
]
