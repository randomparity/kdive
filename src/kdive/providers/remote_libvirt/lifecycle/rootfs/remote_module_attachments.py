"""Fail-closed active and inactive attachment inspection (ADR-0585, ADR-0603)."""

from __future__ import annotations

import os
import posixpath
import re
import stat
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import libvirt
from defusedxml.common import DefusedXmlException

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.xml_bounds import (
    XmlEnumerationBudget,
    parse_libvirt_xml,
)
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS

_DOMAIN_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_MAX_STORAGE_PATH_BYTES = 4096
_MAX_IDENTITY_COMPONENT = (1 << 64) - 1

_NORMALIZED_APPLIANCE_DEVICES = {
    "x86_64": (
        ("controller", (("index", "0"), ("model", "qemu-xhci"), ("ports", "15"), ("type", "usb"))),
        ("controller", (("index", "0"), ("model", "pcie-root"), ("type", "pci"))),
        ("controller", (("index", "0"), ("type", "sata"))),
        ("input", (("bus", "ps2"), ("type", "mouse"))),
        ("input", (("bus", "ps2"), ("type", "keyboard"))),
        ("memballoon", (("model", "virtio"),)),
    ),
    "ppc64le": (
        ("controller", (("index", "0"), ("model", "qemu-xhci"), ("ports", "15"), ("type", "usb"))),
        ("controller", (("index", "0"), ("model", "pci-root"), ("type", "pci"))),
        ("input", (("bus", "usb"), ("type", "mouse"))),
        ("input", (("bus", "usb"), ("type", "keyboard"))),
        ("memballoon", (("model", "virtio"),)),
    ),
}


def normalized_appliance_devices(architecture: str) -> tuple[tuple[str, dict[str, str]], ...]:
    """Return the closed libvirt-normalized device identity for one appliance arch."""
    return tuple(
        (tag, dict(attributes)) for tag, attributes in _NORMALIZED_APPLIANCE_DEVICES[architecture]
    )


@dataclass(frozen=True, slots=True)
class ExpectedAppliance:
    name: str
    architecture: str
    image_digest: str
    operation_nonce: str
    volume: str | None = None
    machine: str | None = None
    memory_kib: int | None = None
    vcpus: int | None = None
    emulator_path: str | None = None


@dataclass(frozen=True, slots=True)
class ExpectedAttachmentState:
    system_id: str
    pool: str
    root_volume: str
    source_volume: str
    scratch_volume: str
    appliance: ExpectedAppliance


@dataclass(frozen=True, slots=True)
class AttachmentInspection:
    system_shut_off: bool
    exclusive: bool
    appliance_present: bool
    detached_volumes: frozenset[tuple[str, str]]

    def proves_detached(self, pool: str, volume: str) -> bool:
        """Return whether this inspection names the exact detached pool/volume pair."""
        return self.system_shut_off and self.exclusive and (pool, volume) in self.detached_volumes


class Domain(Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802
    def isActive(self) -> int: ...  # noqa: N802
    def isPersistent(self) -> int: ...  # noqa: N802


class StorageVolume(Protocol):
    def path(self) -> str: ...


class StoragePool(Protocol):
    def storageVolLookupByName(self, name: str) -> StorageVolume: ...  # noqa: N802


class AttachmentConn(Protocol):
    def listAllDomains(self, flags: int = 0) -> Sequence[Domain]: ...  # noqa: N802
    def storagePoolLookupByName(self, name: str) -> StoragePool: ...  # noqa: N802


@dataclass(frozen=True, slots=True)
class RemoteDeviceIdentity:
    """Opaque physical identity returned by the remote host."""

    kind: Literal["inode", "block"]
    primary: int
    secondary: int


class RemoteDeviceIdentityPort(Protocol):
    """Resolve one remote-host path without returning or logging that path."""

    def identity(self, path: str) -> RemoteDeviceIdentity | None: ...


class HostStatDeviceIdentity:
    """Server-side ADR-0603 adapter backed by a following ``stat(2)`` lookup."""

    def __init__(self, stat_path: Callable[..., os.stat_result] = os.stat) -> None:
        self._stat_path = stat_path

    def identity(self, path: str) -> RemoteDeviceIdentity | None:
        normalized = _normalize_storage_path(path)
        try:
            result = self._stat_path(normalized, follow_symlinks=True)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CategorizedError(
                "remote device identity lookup failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        if stat.S_ISBLK(result.st_mode):
            return RemoteDeviceIdentity(kind="block", primary=result.st_rdev, secondary=0)
        return RemoteDeviceIdentity(kind="inode", primary=result.st_dev, secondary=result.st_ino)


def _conflict(message: str, **details: object) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFLICT, details=details)


def _infrastructure(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.INFRASTRUCTURE_FAILURE)


def inspect_module_attachments(
    conn: AttachmentConn,
    identity_port: RemoteDeviceIdentityPort,
    expected: ExpectedAttachmentState,
) -> AttachmentInspection:
    """Prove the System is stopped and all three volumes have exclusive owners."""
    protected_identities = _protected_volume_identities(conn, identity_port, expected)
    try:
        domains = conn.listAllDomains(0)
    except libvirt.libvirtError as exc:
        raise _infrastructure("could not enumerate remote module attachments") from exc
    owner_domains: set[int] = set()
    xml_budget = XmlEnumerationBudget()
    appliance_present = False
    seen_names: dict[str, int] = {}
    for domain_index, domain in enumerate(domains):
        try:
            active = bool(domain.isActive())
            documents = [(domain.XMLDesc(0), active, active)]
            if active and domain.isPersistent():
                documents.append((domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE), False, active))
        except libvirt.libvirtError as exc:
            raise _infrastructure("could not read a remote module attachment") from exc
        for document, definition_active, domain_active in documents:
            try:
                root = xml_budget.parse(document)
            except (ET.ParseError, DefusedXmlException, ValueError) as exc:
                raise _conflict("could not inspect a remote module attachment") from exc
            name = root.findtext("name")
            if not name:
                raise _conflict("duplicate or unnamed remote module domain", domain=name)
            if name in seen_names and seen_names[name] != domain_index:
                raise _conflict("duplicate or unnamed remote module domain", domain=name)
            seen_names[name] = domain_index
            if _inspect_definition(
                conn,
                identity_port,
                root,
                definition_active,
                domain_active,
                expected,
                protected_identities,
            ):
                owner_domains.add(domain_index)
            if name == expected.appliance.name:
                appliance_present = True
    if len(owner_domains) != 1:
        raise _conflict("expected exactly one owning System definition", count=len(owner_domains))
    detached = frozenset(
        {
            (expected.pool, expected.source_volume),
            (expected.pool, expected.scratch_volume),
        }
        if not appliance_present
        else set()
    )
    return AttachmentInspection(True, True, appliance_present, detached)


def _volume_references(root: ET.Element) -> list[tuple[str, str]]:
    """Return pool/volume pairs from every storage source in each disk graph."""
    references = []
    for disk in root.findall("./devices/disk"):
        for source in disk.iter("source"):
            pool = source.get("pool")
            volume = source.get("volume")
            if pool is not None and volume is not None:
                references.append((pool, volume))
    return references


def _top_level_volume_references(root: ET.Element) -> list[tuple[str, str]]:
    references = []
    for source in root.findall("./devices/disk/source"):
        pool = source.get("pool")
        volume = source.get("volume")
        if pool is not None and volume is not None:
            references.append((pool, volume))
    return references


def _path_references(root: ET.Element) -> set[str]:
    """Return host paths from every source and legacy mirror in each disk graph."""
    paths = set()
    for disk in root.findall("./devices/disk"):
        for source in disk.iter("source"):
            path = source.get("file") or source.get("dev")
            if path is not None:
                paths.add(path)
        for mirror in disk.iter("mirror"):
            path = mirror.get("file") or mirror.get("dev")
            if path is not None:
                paths.add(path)
    return paths


def _volume_path(conn: AttachmentConn, pool_name: str, volume_name: str) -> str:
    try:
        pool = conn.storagePoolLookupByName(pool_name)
        path = pool.storageVolLookupByName(volume_name).path()
    except libvirt.libvirtError as exc:
        raise _infrastructure("could not resolve remote module volume path") from exc
    return _normalize_storage_path(path, pool=pool_name, volume=volume_name)


def _normalize_storage_path(path: str, **details: object) -> str:
    if not path.startswith("/") or "\x00" in path or len(path.encode()) > _MAX_STORAGE_PATH_BYTES:
        raise _conflict("remote module storage path is invalid", **details)
    return posixpath.normpath("/" + path.lstrip("/"))


def _device_identity(identity_port: RemoteDeviceIdentityPort, path: str) -> RemoteDeviceIdentity:
    normalized = _normalize_storage_path(path)
    try:
        identity = identity_port.identity(normalized)
    except Exception as exc:
        raise _infrastructure("remote device identity lookup failed") from exc
    if identity is None:
        raise _conflict("remote device identity is unavailable")
    if (
        type(identity) is not RemoteDeviceIdentity
        or identity.kind not in {"inode", "block"}
        or type(identity.primary) is not int
        or type(identity.secondary) is not int
        or not 0 <= identity.primary <= _MAX_IDENTITY_COMPONENT
        or not 0 <= identity.secondary <= _MAX_IDENTITY_COMPONENT
        or (identity.kind == "block" and identity.secondary != 0)
    ):
        raise _conflict("remote device identity is invalid")
    return identity


def _protected_volume_identities(
    conn: AttachmentConn,
    identity_port: RemoteDeviceIdentityPort,
    expected: ExpectedAttachmentState,
) -> dict[str, RemoteDeviceIdentity]:
    identities = {
        volume: _device_identity(identity_port, _volume_path(conn, expected.pool, volume))
        for volume in (expected.root_volume, expected.source_volume, expected.scratch_volume)
    }
    if len(set(identities.values())) != len(identities):
        raise _conflict("protected remote module identities are not distinct")
    return identities


def _inspect_definition(
    conn: AttachmentConn,
    identity_port: RemoteDeviceIdentityPort,
    root: ET.Element,
    definition_active: bool,
    domain_active: bool,
    expected: ExpectedAttachmentState,
    protected_identities: dict[str, RemoteDeviceIdentity],
) -> bool:
    name = root.findtext("name")
    protected = {expected.root_volume, expected.source_volume, expected.scratch_volume}
    # Scoped to pool/volume pairs on purpose: a file-, block-, or network-backed
    # disk carries no pool or volume attribute, so keying the duplicate guard on
    # every source made two ordinary disks on any unrelated tenant collide and
    # fail every operation on the host closed.
    sources = _volume_references(root)
    if len(sources) != len(set(sources)):
        raise _conflict("duplicate volume reference in domain", domain=name)
    referenced = {volume for pool, volume in sources if pool == expected.pool}
    direct_identities = {_device_identity(identity_port, path) for path in _path_references(root)}
    system_tags = root.findall(f"./metadata/{{{KDIVE_METADATA_NS}}}system")
    if len(system_tags) > 1:
        raise _conflict("duplicate System ownership metadata", domain=name)
    system_tag = system_tags[0].text if system_tags else None
    if system_tag == expected.system_id:
        resolved_identities = [
            _device_identity(identity_port, _volume_path(conn, pool, volume))
            for pool, volume in sources
        ]
        protected_references = (set(resolved_identities) | direct_identities) & set(
            protected_identities.values()
        )
        if domain_active:
            raise _conflict("owning System is active", domain=name)
        owning_root = (expected.pool, expected.root_volume)
        if _top_level_volume_references(root).count(owning_root) != 1:
            raise _conflict("owning System definition has a different root volume", domain=name)
        attempt_identities = {
            protected_identities[expected.source_volume],
            protected_identities[expected.scratch_volume],
        }
        if referenced & {expected.source_volume, expected.scratch_volume} or (
            protected_references & attempt_identities
        ):
            raise _conflict("System references attempt-scoped appliance storage", domain=name)
        root_identity = protected_identities[expected.root_volume]
        root_count = resolved_identities.count(root_identity) + list(direct_identities).count(
            root_identity
        )
        if root_count != 1:
            raise _conflict("owning System has duplicate root volume references", domain=name)
        return True
    if name == expected.appliance.name:
        _validate_appliance(root, definition_active, expected)
        return False
    resolved_identities = [
        _device_identity(identity_port, _volume_path(conn, pool, volume))
        for pool, volume in sources
    ]
    protected_references = (set(resolved_identities) | direct_identities) & set(
        protected_identities.values()
    )
    if referenced & protected:
        raise _conflict("another domain references remote module storage", domain=name)
    if protected_references:
        raise _conflict("another domain references remote module storage by path", domain=name)
    return False


def _validate_appliance(
    root: ET.Element,
    active: bool,
    expected: ExpectedAttachmentState,
) -> None:
    metadata_nodes = root.findall("./metadata/remote-module-appliance")
    type_node = root.find("./os/type")
    _validate_appliance_resources(root, expected)
    required_disks = (
        {
            (expected.pool, expected.appliance.volume, "vda", True),
            (expected.pool, expected.root_volume, "vdb", False),
            (expected.pool, expected.source_volume, "vdc", True),
            (expected.pool, expected.scratch_volume, "vdd", False),
        }
        if expected.appliance.volume is not None
        else {
            (expected.pool, expected.root_volume, "vda", False),
            (expected.pool, expected.source_volume, "vdb", True),
            (expected.pool, expected.scratch_volume, "vdc", False),
        }
    )
    if len(metadata_nodes) != 1 or type_node is None:
        raise _conflict("resumed appliance metadata is absent")
    metadata = metadata_nodes[0]
    if metadata.attrib != {
        "system": expected.system_id,
        "image-digest": expected.appliance.image_digest,
        "nonce": expected.appliance.operation_nonce,
    }:
        raise _conflict("resumed appliance metadata mismatched")
    if type_node.get("arch") != expected.appliance.architecture:
        raise _conflict("resumed appliance architecture mismatched")
    disks = []
    for disk in root.findall("./devices/disk"):
        if disk.attrib != {"type": "volume", "device": "disk"}:
            raise _conflict("resumed appliance volume set mismatched")
        children = list(disk)
        allowed_tags = {"driver", "source", "target", "readonly", "alias", "address"}
        if any(child.tag not in allowed_tags for child in children):
            raise _conflict("resumed appliance volume set mismatched")
        source_nodes = disk.findall("source")
        target_nodes = disk.findall("target")
        if len(source_nodes) != 1 or len(target_nodes) != 1:
            raise _conflict("resumed appliance volume set mismatched")
        source = source_nodes[0]
        target = target_nodes[0]
        source_extra = set(source.attrib) - {"pool", "volume", "index", "startupPolicy"}
        if not {"pool", "volume"}.issubset(source.attrib) or source_extra or list(source):
            raise _conflict("resumed appliance volume set mismatched")
        target_extra = set(target.attrib) - {"dev", "bus", "tray"}
        if target.get("bus") != "virtio" or target_extra or list(target):
            raise _conflict("resumed appliance volume set mismatched")
        readonly = disk.find("readonly")
        if readonly is not None and (readonly.attrib or list(readonly)):
            raise _conflict("resumed appliance volume set mismatched")
        driver = disk.find("driver")
        driver_attrs = {"name", "type", "cache", "io", "discard"}
        if driver is not None and (set(driver.attrib) - driver_attrs or list(driver)):
            raise _conflict("resumed appliance volume set mismatched")
        alias = disk.find("alias")
        if alias is not None and (set(alias.attrib) - {"name"} or list(alias)):
            raise _conflict("resumed appliance volume set mismatched")
        address = disk.find("address")
        address_attrs = {
            "type",
            "domain",
            "bus",
            "slot",
            "function",
            "controller",
            "target",
            "unit",
        }
        if address is not None and (set(address.attrib) - address_attrs or list(address)):
            raise _conflict("resumed appliance volume set mismatched")
        disks.append(
            (
                source.get("pool"),
                source.get("volume"),
                target.get("dev"),
                disk.find("readonly") is not None,
            )
        )
    if set(disks) != required_disks or len(disks) != len(required_disks):
        raise _conflict("resumed appliance volume set mismatched")
    _validate_appliance_devices(root, expected)
    if not active:
        raise _conflict("same-name resumed appliance is not active")


def _validate_appliance_resources(root: ET.Element, expected: ExpectedAttachmentState) -> None:
    if (
        expected.appliance.machine is None
        and expected.appliance.memory_kib is None
        and expected.appliance.vcpus is None
    ):
        return
    if root.get("type") != "kvm" or set(root.attrib) - {"type", "id"}:
        raise _conflict("resumed appliance domain type mismatched")
    top_level = {"name", "uuid", "memory", "vcpu", "os", "metadata", "devices"}
    if any(child.tag not in top_level for child in root):
        raise _conflict("resumed appliance top-level shape mismatched")
    required_once = {"name", "memory", "vcpu", "os", "metadata", "devices"}
    if any(len(root.findall(tag)) != 1 for tag in required_once):
        raise _conflict("resumed appliance top-level shape mismatched")
    name = root.find("name")
    if (
        name is None
        or name.attrib
        or list(name)
        or (name.text or "").strip() != expected.appliance.name
    ):
        raise _conflict("resumed appliance name mismatched")
    uuid_nodes = root.findall("uuid")
    if len(uuid_nodes) > 1 or any(
        node.attrib or list(node) or _DOMAIN_UUID.fullmatch((node.text or "").strip()) is None
        for node in uuid_nodes
    ):
        raise _conflict("resumed appliance UUID normalization mismatched")
    os_nodes = root.findall("os")
    if os_nodes[0].attrib or [child.tag for child in os_nodes[0]] != ["type"]:
        raise _conflict("resumed appliance OS shape mismatched")
    type_nodes = root.findall("./os/type")
    if len(type_nodes) != 1:
        raise _conflict("resumed appliance OS type mismatched")
    type_node = type_nodes[0]
    if (type_node.text or "").strip() != "hvm":
        raise _conflict("resumed appliance OS type mismatched")
    expected_type = {"arch": expected.appliance.architecture}
    if expected.appliance.machine is not None:
        expected_type["machine"] = expected.appliance.machine
    if type_node.attrib != expected_type:
        raise _conflict("resumed appliance OS type mismatched")
    if expected.appliance.memory_kib is not None:
        memory_nodes = root.findall("./memory")
        if (
            len(memory_nodes) != 1
            or memory_nodes[0].attrib != {"unit": "KiB"}
            or list(memory_nodes[0])
            or (memory_nodes[0].text or "").strip() != str(expected.appliance.memory_kib)
        ):
            raise _conflict("resumed appliance memory mismatched")
    if expected.appliance.vcpus is not None:
        vcpu_nodes = root.findall("./vcpu")
        if len(vcpu_nodes) != 1:
            raise _conflict("resumed appliance vCPU count mismatched")
        vcpu = vcpu_nodes[0]
        if set(vcpu.attrib) - {"placement", "current"} or list(vcpu):
            raise _conflict("resumed appliance vCPU count mismatched")
        if (vcpu.text or "").strip() != str(expected.appliance.vcpus):
            raise _conflict("resumed appliance vCPU count mismatched")
        if vcpu.get("current") not in {None, str(expected.appliance.vcpus)}:
            raise _conflict("resumed appliance vCPU count mismatched")


def _validate_appliance_devices(root: ET.Element, expected: ExpectedAttachmentState) -> None:
    devices = root.find("./devices")
    if devices is None:
        raise _conflict("resumed appliance devices are absent")
    allowed = {"disk", "console", "controller", "input", "memballoon", "emulator"}
    if any(device.tag not in allowed for device in devices):
        raise _conflict("resumed appliance unexpectedly has a forbidden device")
    for device in devices:
        if device.tag not in {"disk", "console"}:
            _validate_normalized_device(device)
    normalized = [
        device for device in devices if device.tag in {"controller", "input", "memballoon"}
    ]
    if expected.appliance.emulator_path is None:
        controllers = [
            (device.get("type"), device.get("index")) for device in devices.findall("controller")
        ]
        inputs = [(device.get("type"), device.get("bus")) for device in devices.findall("input")]
        if len(controllers) != len(set(controllers)) or len(inputs) != len(set(inputs)):
            raise _conflict("resumed appliance normalized device duplicated")
    else:
        actual_identities = Counter(
            (device.tag, tuple(sorted(device.attrib.items()))) for device in normalized
        )
        expected_identities = Counter(
            (tag, tuple(sorted(attributes.items())))
            for tag, attributes in normalized_appliance_devices(expected.appliance.architecture)
        )
        if actual_identities != expected_identities:
            raise _conflict("resumed appliance normalized device set mismatched")
        emulators = devices.findall("emulator")
        if len(emulators) != 1:
            raise _conflict("resumed appliance emulator mismatched")
        if (emulators[0].text or "").strip() != expected.appliance.emulator_path:
            raise _conflict("resumed appliance emulator mismatched")
    consoles = devices.findall("console")
    if len(consoles) != 1:
        raise _conflict("resumed appliance console set mismatched")
    console = consoles[0]
    if console.attrib != {"type": "pty"}:
        raise _conflict("resumed appliance console transport mismatched")
    child_allowlist = {"source", "target", "alias", "address"}
    if any(child.tag not in child_allowlist or list(child) for child in console):
        raise _conflict("resumed appliance console set mismatched")
    sources = console.findall("source")
    if len(sources) > 1 or any(set(source.attrib) - {"path"} for source in sources):
        raise _conflict("resumed appliance console set mismatched")
    targets = console.findall("target")
    if len(targets) > 1:
        raise _conflict("resumed appliance console set mismatched")
    if targets and (
        set(targets[0].attrib) - {"type", "port"} or targets[0].get("type") not in {None, "serial"}
    ):
        raise _conflict("resumed appliance console set mismatched")
    for alias in console.findall("alias"):
        if set(alias.attrib) - {"name"}:
            raise _conflict("resumed appliance console set mismatched")
    address_attrs = {"type", "controller", "bus", "port"}
    for address in console.findall("address"):
        if set(address.attrib) - address_attrs:
            raise _conflict("resumed appliance console set mismatched")


def _validate_normalized_device(device: ET.Element) -> None:
    attribute_allowlists = {
        "controller": {"type", "index", "model", "ports"},
        "input": {"type", "bus", "model"},
        "memballoon": {"model", "autodeflate", "freePageReporting"},
        "emulator": set(),
    }
    child_allowlists = {
        "controller": {"alias", "address"},
        "input": {"alias", "address"},
        "memballoon": {"stats", "alias", "address"},
        "emulator": set(),
    }
    if set(device.attrib) - attribute_allowlists[device.tag]:
        raise _conflict("resumed appliance normalized device mismatched")
    if any(child.tag not in child_allowlists[device.tag] or list(child) for child in device):
        raise _conflict("resumed appliance normalized device mismatched")
    child_attributes = {
        "alias": {"name"},
        "address": {
            "type",
            "domain",
            "bus",
            "slot",
            "function",
            "controller",
            "target",
            "unit",
            "port",
        },
        "stats": {"period"},
    }
    if any(set(child.attrib) - child_attributes[child.tag] for child in device):
        raise _conflict("resumed appliance normalized device mismatched")
    child_counts = {tag: len(device.findall(tag)) for tag in child_allowlists[device.tag]}
    if any(count > 1 for count in child_counts.values()):
        raise _conflict("resumed appliance normalized device mismatched")
    if device.tag == "controller" and device.get("type") not in {
        "pci",
        "usb",
        "sata",
        "virtio-serial",
    }:
        raise _conflict("resumed appliance normalized device mismatched")
    if device.tag == "input" and (
        device.get("type") not in {"mouse", "keyboard", "tablet"}
        or device.get("bus") not in {"ps2", "usb", "virtio"}
    ):
        raise _conflict("resumed appliance normalized device mismatched")
    if device.tag == "memballoon" and device.get("model") not in {"virtio", "none"}:
        raise _conflict("resumed appliance normalized device mismatched")
    if device.tag == "emulator" and (device.text or "").strip() == "":
        raise _conflict("resumed appliance emulator mismatched")


def validate_appliance_xml(xml: str, expected: ExpectedAttachmentState) -> None:
    """Validate an active appliance definition while tolerating libvirt-owned normalization."""
    try:
        root = parse_libvirt_xml(xml)
    except (ET.ParseError, DefusedXmlException, ValueError) as exc:
        raise _conflict("could not parse resumed appliance definition") from exc
    if root.findtext("name") != expected.appliance.name:
        raise _conflict("resumed appliance name mismatched")
    _validate_appliance(root, True, expected)
