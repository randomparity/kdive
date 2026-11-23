"""Fail-closed active and inactive attachment inspection (ADR-0585, ADR-0603)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass

import libvirt
from defusedxml.common import DefusedXmlException

from kdive.providers.ports.module_operation import RemoteDeviceIdentity, RemoteDeviceIdentityPort
from kdive.providers.shared.libvirt_attachments import (
    MAX_PATH_IDENTITIES,
    AttachmentConn,
    attachment_conflict,
    attachment_infrastructure,
    device_identity,
    path_reference_list,
    path_references,
    system_ownership,
    volume_path,
    volume_references,
)
from kdive.providers.shared.libvirt_xml import (
    backing_store_projection,
)
from kdive.providers.shared.libvirt_xml_bounds import (
    XmlEnumerationBudget,
    parse_libvirt_xml,
)

_DOMAIN_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)

# Kernel command line of the direct-boot appliance; domain-v1-<arch>.xml carries the same value.
# PYTHONHASHSEED reaches /init as an environment variable: the default qemu64 guest has no RDRAND,
# so without it Python cannot seed its hash and the init dies before it can report anything.
APPLIANCE_KERNEL_CMDLINE = "console=hvc0 panic=-1 PYTHONHASHSEED=0"

_NORMALIZED_APPLIANCE_DEVICES = {
    "x86_64": (
        ("controller", (("index", "0"), ("model", "qemu-xhci"), ("ports", "15"), ("type", "usb"))),
        ("controller", (("index", "0"), ("model", "pcie-root"), ("type", "pci"))),
        ("controller", (("index", "0"), ("type", "sata"))),
        ("controller", (("index", "0"), ("type", "virtio-serial"))),
        ("input", (("bus", "ps2"), ("type", "mouse"))),
        ("input", (("bus", "ps2"), ("type", "keyboard"))),
        ("memballoon", (("model", "none"),)),
    ),
    "ppc64le": (
        ("controller", (("index", "0"), ("model", "qemu-xhci"), ("ports", "15"), ("type", "usb"))),
        ("controller", (("index", "0"), ("model", "pci-root"), ("type", "pci"))),
        ("controller", (("index", "0"), ("type", "virtio-serial"))),
        ("input", (("bus", "usb"), ("type", "mouse"))),
        ("input", (("bus", "usb"), ("type", "keyboard"))),
        ("memballoon", (("model", "none"),)),
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
    kernel: str | None = None
    initrd: str | None = None
    root_backing_path: str | None = None


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

    def proves_unattached(self, pool: str, volume: str) -> bool:
        """Return whether the pair is attached to no domain, whatever the System's power.

        Only deleting an attempt volume may rely on this: it needs the volume unreferenced, not
        the System off. Starting the appliance or restoring still needs ``proves_detached``.
        """
        return self.exclusive and (pool, volume) in self.detached_volumes


def inspect_module_attachments(
    conn: AttachmentConn,
    identity_port: RemoteDeviceIdentityPort,
    expected: ExpectedAttachmentState,
    present_attempt_volumes: frozenset[str] | None = None,
    *,
    allow_cleanup_partial: bool = False,
    allow_active_system: bool = False,
) -> AttachmentInspection:
    """Prove the System is stopped and all three volumes have exclusive owners.

    ``allow_active_system`` admits a running owning System for a cleanup that only deletes
    attempt volumes. The inspection then reports ``system_shut_off=False`` and still refuses a
    System whose definition references any of those volumes.
    """
    protected_identities = _protected_volume_identities(
        conn,
        identity_port,
        expected,
        present_attempt_volumes,
        allow_cleanup_partial=allow_cleanup_partial,
    )
    try:
        domains = conn.listAllDomains(0)
    except libvirt.libvirtError as exc:
        raise attachment_infrastructure("could not enumerate remote module attachments") from exc
    owner_domains: set[int] = set()
    system_active = False
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
            raise attachment_infrastructure("could not read a remote module attachment") from exc
        for document, definition_active, domain_active in documents:
            try:
                root = xml_budget.parse(document)
            except (ET.ParseError, DefusedXmlException, ValueError) as exc:
                raise attachment_conflict("could not inspect a remote module attachment") from exc
            name = root.findtext("name")
            if not name:
                raise attachment_conflict("duplicate or unnamed remote module domain", domain=name)
            if name in seen_names and seen_names[name] != domain_index:
                raise attachment_conflict("duplicate or unnamed remote module domain", domain=name)
            seen_names[name] = domain_index
            if _inspect_definition(
                conn,
                identity_port,
                root,
                definition_active,
                domain_active,
                expected,
                protected_identities,
                allow_active_system=allow_active_system,
            ):
                owner_domains.add(domain_index)
                system_active = system_active or domain_active
            if name == expected.appliance.name:
                appliance_present = True
    if len(owner_domains) != 1:
        raise attachment_conflict(
            "expected exactly one owning System definition", count=len(owner_domains)
        )
    detached = frozenset(
        {
            (expected.pool, expected.source_volume),
            (expected.pool, expected.scratch_volume),
        }
        if not appliance_present
        else set()
    )
    return AttachmentInspection(not system_active, True, appliance_present, detached)


def _top_level_volume_references(root: ET.Element) -> list[tuple[str, str]]:
    references = []
    for source in root.findall("./devices/disk/source"):
        pool = source.get("pool")
        volume = source.get("volume")
        if pool is not None and volume is not None:
            references.append((pool, volume))
    return references


def _top_level_path_references(root: ET.Element) -> list[str]:
    """File- and block-backed top-level disk sources, one per disk, backing chains excluded."""
    paths = (
        source.get("file") or source.get("dev") for source in root.findall("./devices/disk/source")
    )
    return [path for path in paths if path is not None]


def _protected_volume_identities(
    conn: AttachmentConn,
    identity_port: RemoteDeviceIdentityPort,
    expected: ExpectedAttachmentState,
    present_attempt_volumes: frozenset[str] | None = None,
    *,
    allow_cleanup_partial: bool = False,
) -> dict[str, RemoteDeviceIdentity]:
    attempt_volumes = (
        frozenset({expected.source_volume, expected.scratch_volume})
        if present_attempt_volumes is None
        else present_attempt_volumes
    )
    allowed = {
        frozenset({expected.source_volume}),
        frozenset({expected.source_volume, expected.scratch_volume}),
    }
    if allow_cleanup_partial:
        allowed.add(frozenset({expected.scratch_volume}))
    if attempt_volumes not in allowed:
        raise attachment_conflict("remote module partial volume state is invalid")
    identities = {
        volume: device_identity(identity_port, volume_path(conn, expected.pool, volume))
        for volume in (expected.root_volume, *sorted(attempt_volumes))
    }
    if len(set(identities.values())) != len(identities):
        raise attachment_conflict("protected remote module identities are not distinct")
    return identities


def _inspect_definition(
    conn: AttachmentConn,
    identity_port: RemoteDeviceIdentityPort,
    root: ET.Element,
    definition_active: bool,
    domain_active: bool,
    expected: ExpectedAttachmentState,
    protected_identities: dict[str, RemoteDeviceIdentity],
    *,
    allow_active_system: bool = False,
) -> bool:
    name = root.findtext("name")
    protected = {expected.root_volume, expected.source_volume, expected.scratch_volume}
    # Scoped to pool/volume pairs on purpose: a file-, block-, or network-backed
    # disk carries no pool or volume attribute, so keying the duplicate guard on
    # every source made two ordinary disks on any unrelated tenant collide and
    # fail every operation on the host closed.
    sources = volume_references(root)
    if len(sources) != len(set(sources)):
        raise attachment_conflict("duplicate volume reference in domain", domain=name)
    referenced = {volume for pool, volume in sources if pool == expected.pool}
    paths = path_references(root)
    if len(paths) > MAX_PATH_IDENTITIES:
        raise attachment_infrastructure("remote device identity lookup budget exceeded")
    direct_identities = {device_identity(identity_port, path) for path in paths}
    system_tag = system_ownership(root, domain=name)
    if system_tag == expected.system_id:
        resolved_identities = [
            device_identity(identity_port, volume_path(conn, pool, volume))
            for pool, volume in sources
        ]
        protected_references = (set(resolved_identities) | direct_identities) & set(
            protected_identities.values()
        )
        if domain_active and not allow_active_system:
            raise attachment_conflict("owning System is active", domain=name)
        owning_root = (expected.pool, expected.root_volume)
        root_identity = protected_identities[expected.root_volume]
        # A provisioned System's root is a file-backed overlay with no pool/volume source, so
        # it counts by device identity; the same root named any other way still counts twice.
        root_references = _top_level_volume_references(root).count(owning_root) + sum(
            device_identity(identity_port, path) == root_identity
            for path in _top_level_path_references(root)
        )
        if root_references != 1:
            raise attachment_conflict(
                "owning System definition has a different root volume", domain=name
            )
        attempt_identities = {
            identity
            for volume, identity in protected_identities.items()
            if volume != expected.root_volume
        }
        if referenced & {expected.source_volume, expected.scratch_volume} or (
            protected_references & attempt_identities
        ):
            raise attachment_conflict(
                "System references attempt-scoped appliance storage", domain=name
            )
        root_count = resolved_identities.count(root_identity) + sum(
            device_identity(identity_port, path) == root_identity
            for path in path_reference_list(root)
        )
        if root_count != 1:
            raise attachment_conflict(
                "owning System has duplicate root volume references", domain=name
            )
        return True
    if name == expected.appliance.name:
        _validate_appliance(root, definition_active, expected)
        return False
    resolved_identities = [
        device_identity(identity_port, volume_path(conn, pool, volume)) for pool, volume in sources
    ]
    protected_references = (set(resolved_identities) | direct_identities) & set(
        protected_identities.values()
    )
    if referenced & protected:
        raise attachment_conflict("another domain references remote module storage", domain=name)
    if protected_references:
        raise attachment_conflict(
            "another domain references remote module storage by path", domain=name
        )
    return False


def _validate_features(root: ET.Element, architecture: str) -> None:
    """x86_64 needs exactly ``<features><acpi/></features>``; other architectures have none."""
    nodes = root.findall("features")
    if architecture != "x86_64":
        if nodes:
            raise attachment_conflict("resumed appliance features mismatched")
        return
    if len(nodes) != 1 or nodes[0].attrib or nodes[0].text and nodes[0].text.strip():
        raise attachment_conflict("resumed appliance features mismatched")
    children = list(nodes[0])
    if len(children) != 1 or children[0].tag != "acpi" or children[0].attrib or list(children[0]):
        raise attachment_conflict("resumed appliance features mismatched")


def _validate_disk_backing(
    disk: ET.Element, dev: str | None, expected: ExpectedAttachmentState
) -> None:
    """Accept libvirt's empty terminator anywhere; the root's typed backing must be the base.

    A qcow2 root is dumped with a typed ``<backingStore>`` naming the shared staged base. When the
    domain was rendered with an explicit one, it must still carry the ``selinux`` ``relabel="no"``
    seclabel that stops libvirt relabelling the base (ADR-0623); every other disk carries only the
    empty terminator.
    """
    backings = disk.findall("backingStore")
    wanted = expected.appliance.root_backing_path if expected.appliance.volume is None else None
    is_root = dev == "vda"
    if len(backings) > 1 or (not backings and is_root and wanted is not None):
        raise attachment_conflict("resumed appliance volume set mismatched")
    if not backings:
        return
    backing = backings[0]
    if not backing.attrib and not list(backing):
        if is_root and wanted is not None:
            raise attachment_conflict("resumed appliance volume set mismatched")
        return
    if not is_root or wanted is None or not _is_staged_base_backing(backing, disk, wanted):
        raise attachment_conflict("resumed appliance volume set mismatched")


def _is_staged_base_backing(backing: ET.Element, disk: ET.Element, path: str) -> bool:
    source = backing.find("source")
    format_node = backing.find("format")
    terminator = backing.find("backingStore")
    if source is None or format_node is None or terminator is None:
        return False
    seclabels = source.findall("seclabel")
    return (
        {child.tag for child in backing} == {"format", "source", "backingStore"}
        and len(list(backing)) == 3
        and set(backing.attrib) <= {"type", "index"}
        and format_node.attrib == {"type": "qcow2"}
        and not list(format_node)
        and set(source.attrib) == {"file"}
        and [child.tag for child in source] == ["seclabel"]
        and len(seclabels) == 1
        and seclabels[0].attrib == {"model": "selinux", "relabel": "no"}
        and not list(seclabels[0])
        and not terminator.attrib
        and not list(terminator)
        and backing_store_projection(disk) == ("file", "qcow2", path, (("selinux", "no", None),))
    )


_DIRECT_KERNEL_DISK_FORMATS = {"vda": "qcow2", "vdb": "raw", "vdc": "raw"}


def _has_reference_driver(dev: str | None, driver: ET.Element | None) -> bool:
    """Whether a direct-kernel disk carries the reference driver for its role.

    The System root is a qcow2 overlay and the other two volumes are raw; a missing or different
    driver hands the guest the wrong bytes (a qcow2 container read as a raw disk).
    """
    image_format = _DIRECT_KERNEL_DISK_FORMATS.get(dev or "")
    return (
        image_format is not None
        and driver is not None
        and driver.attrib == {"name": "qemu", "type": image_format, "cache": "none", "io": "native"}
    )


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
        raise attachment_conflict("resumed appliance metadata is absent")
    metadata = metadata_nodes[0]
    if metadata.attrib != {
        "system": expected.system_id,
        "image-digest": expected.appliance.image_digest,
        "nonce": expected.appliance.operation_nonce,
    }:
        raise attachment_conflict("resumed appliance metadata mismatched")
    if type_node.get("arch") != expected.appliance.architecture:
        raise attachment_conflict("resumed appliance architecture mismatched")
    disks = []
    for disk in root.findall("./devices/disk"):
        if disk.attrib != {"type": "volume", "device": "disk"}:
            raise attachment_conflict("resumed appliance volume set mismatched")
        children = list(disk)
        allowed_tags = {
            "driver",
            "source",
            "backingStore",
            "target",
            "readonly",
            "alias",
            "address",
        }
        if any(child.tag not in allowed_tags for child in children):
            raise attachment_conflict("resumed appliance volume set mismatched")
        source_nodes = disk.findall("source")
        target_nodes = disk.findall("target")
        if len(source_nodes) != 1 or len(target_nodes) != 1:
            raise attachment_conflict("resumed appliance volume set mismatched")
        source = source_nodes[0]
        target = target_nodes[0]
        source_extra = set(source.attrib) - {"pool", "volume", "index", "startupPolicy"}
        if not {"pool", "volume"}.issubset(source.attrib) or source_extra or list(source):
            raise attachment_conflict("resumed appliance volume set mismatched")
        target_extra = set(target.attrib) - {"dev", "bus", "tray"}
        if target.get("bus") != "virtio" or target_extra or list(target):
            raise attachment_conflict("resumed appliance volume set mismatched")
        readonly = disk.find("readonly")
        if readonly is not None and (readonly.attrib or list(readonly)):
            raise attachment_conflict("resumed appliance volume set mismatched")
        driver = disk.find("driver")
        driver_attrs = {"name", "type", "cache", "io", "discard"}
        if driver is not None and (set(driver.attrib) - driver_attrs or list(driver)):
            raise attachment_conflict("resumed appliance volume set mismatched")
        if expected.appliance.volume is None and not _has_reference_driver(
            target.get("dev"), driver
        ):
            raise attachment_conflict("resumed appliance volume set mismatched")
        _validate_disk_backing(disk, target.get("dev"), expected)
        alias = disk.find("alias")
        if alias is not None and (set(alias.attrib) - {"name"} or list(alias)):
            raise attachment_conflict("resumed appliance volume set mismatched")
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
            raise attachment_conflict("resumed appliance volume set mismatched")
        disks.append(
            (
                source.get("pool"),
                source.get("volume"),
                target.get("dev"),
                disk.find("readonly") is not None,
            )
        )
    if set(disks) != required_disks or len(disks) != len(required_disks):
        raise attachment_conflict("resumed appliance volume set mismatched")
    _validate_appliance_devices(root, expected)
    if not active:
        raise attachment_conflict("same-name resumed appliance is not active")


def _validate_appliance_resources(root: ET.Element, expected: ExpectedAttachmentState) -> None:
    if (
        expected.appliance.machine is None
        and expected.appliance.memory_kib is None
        and expected.appliance.vcpus is None
    ):
        return
    if root.get("type") != "kvm" or set(root.attrib) - {"type", "id"}:
        raise attachment_conflict("resumed appliance domain type mismatched")
    lifecycle = ("on_poweroff", "on_reboot", "on_crash")
    top_level = {"name", "uuid", "memory", "vcpu", "os", "metadata", "devices", "features"}
    top_level.update(lifecycle)
    if any(child.tag not in top_level for child in root):
        raise attachment_conflict("resumed appliance top-level shape mismatched")
    _validate_features(root, expected.appliance.architecture)
    for tag in lifecycle:
        nodes = root.findall(tag)
        if len(nodes) != 1 or any(
            node.attrib or list(node) or (node.text or "").strip() != "destroy" for node in nodes
        ):
            raise attachment_conflict("resumed appliance lifecycle action mismatched")
    required_once = {"name", "memory", "vcpu", "os", "metadata", "devices"}
    if any(len(root.findall(tag)) != 1 for tag in required_once):
        raise attachment_conflict("resumed appliance top-level shape mismatched")
    name = root.find("name")
    if (
        name is None
        or name.attrib
        or list(name)
        or (name.text or "").strip() != expected.appliance.name
    ):
        raise attachment_conflict("resumed appliance name mismatched")
    uuid_nodes = root.findall("uuid")
    if len(uuid_nodes) > 1 or any(
        node.attrib or list(node) or _DOMAIN_UUID.fullmatch((node.text or "").strip()) is None
        for node in uuid_nodes
    ):
        raise attachment_conflict("resumed appliance UUID normalization mismatched")
    os_nodes = root.findall("os")
    expected_os_children = (
        ["type"]
        if expected.appliance.volume is not None
        else ["type", "kernel", "initrd", "cmdline"]
    )
    if os_nodes[0].attrib or [child.tag for child in os_nodes[0]] != expected_os_children:
        raise attachment_conflict("resumed appliance OS shape mismatched")
    type_nodes = root.findall("./os/type")
    if len(type_nodes) != 1:
        raise attachment_conflict("resumed appliance OS type mismatched")
    type_node = type_nodes[0]
    if (type_node.text or "").strip() != "hvm":
        raise attachment_conflict("resumed appliance OS type mismatched")
    expected_type = {"arch": expected.appliance.architecture}
    if expected.appliance.machine is not None:
        expected_type["machine"] = expected.appliance.machine
    if type_node.attrib != expected_type:
        raise attachment_conflict("resumed appliance OS type mismatched")
    if expected.appliance.volume is None:
        kernel = root.find("./os/kernel")
        initrd = root.find("./os/initrd")
        if (
            kernel is None
            or initrd is None
            or kernel.attrib
            or initrd.attrib
            or list(kernel)
            or list(initrd)
            or (kernel.text or "") != expected.appliance.kernel
            or (initrd.text or "") != expected.appliance.initrd
        ):
            raise attachment_conflict("resumed appliance direct kernel mismatched")
        cmdline = root.find("./os/cmdline")
        if (
            cmdline is None
            or cmdline.attrib
            or list(cmdline)
            or (cmdline.text or "") != APPLIANCE_KERNEL_CMDLINE
        ):
            raise attachment_conflict("resumed appliance kernel command line mismatched")
    if expected.appliance.memory_kib is not None:
        memory_nodes = root.findall("./memory")
        if (
            len(memory_nodes) != 1
            or memory_nodes[0].attrib != {"unit": "KiB"}
            or list(memory_nodes[0])
            or (memory_nodes[0].text or "").strip() != str(expected.appliance.memory_kib)
        ):
            raise attachment_conflict("resumed appliance memory mismatched")
    if expected.appliance.vcpus is not None:
        vcpu_nodes = root.findall("./vcpu")
        if len(vcpu_nodes) != 1:
            raise attachment_conflict("resumed appliance vCPU count mismatched")
        vcpu = vcpu_nodes[0]
        if set(vcpu.attrib) - {"placement", "current"} or list(vcpu):
            raise attachment_conflict("resumed appliance vCPU count mismatched")
        if (vcpu.text or "").strip() != str(expected.appliance.vcpus):
            raise attachment_conflict("resumed appliance vCPU count mismatched")
        if vcpu.get("current") not in {None, str(expected.appliance.vcpus)}:
            raise attachment_conflict("resumed appliance vCPU count mismatched")


def _validate_appliance_devices(root: ET.Element, expected: ExpectedAttachmentState) -> None:
    devices = root.find("./devices")
    if devices is None:
        raise attachment_conflict("resumed appliance devices are absent")
    allowed = {"disk", "console", "controller", "input", "memballoon", "emulator", "rng"}
    if any(device.tag not in allowed for device in devices):
        raise attachment_conflict("resumed appliance unexpectedly has a forbidden device")
    _validate_rng(devices)
    for device in devices:
        if device.tag not in {"disk", "console", "rng"}:
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
            raise attachment_conflict("resumed appliance normalized device duplicated")
    else:
        actual_identities = Counter(
            (device.tag, tuple(sorted(device.attrib.items()))) for device in normalized
        )
        expected_identities = Counter(
            (tag, tuple(sorted(attributes.items())))
            for tag, attributes in normalized_appliance_devices(expected.appliance.architecture)
        )
        if actual_identities != expected_identities:
            raise attachment_conflict("resumed appliance normalized device set mismatched")
        emulators = devices.findall("emulator")
        if len(emulators) != 1:
            raise attachment_conflict("resumed appliance emulator mismatched")
        if (emulators[0].text or "").strip() != expected.appliance.emulator_path:
            raise attachment_conflict("resumed appliance emulator mismatched")
    consoles = devices.findall("console")
    if len(consoles) != 1:
        raise attachment_conflict("resumed appliance console set mismatched")
    console = consoles[0]
    if console.attrib != {"type": "pty"}:
        raise attachment_conflict("resumed appliance console transport mismatched")
    child_allowlist = {"source", "target", "alias", "address"}
    if any(child.tag not in child_allowlist or list(child) for child in console):
        raise attachment_conflict("resumed appliance console set mismatched")
    sources = console.findall("source")
    if len(sources) > 1 or any(set(source.attrib) - {"path"} for source in sources):
        raise attachment_conflict("resumed appliance console set mismatched")
    targets = console.findall("target")
    if len(targets) != 1 or (
        set(targets[0].attrib) - {"type", "port"} or targets[0].get("type") != "virtio"
    ):
        raise attachment_conflict("resumed appliance console set mismatched")
    for alias in console.findall("alias"):
        if set(alias.attrib) - {"name"}:
            raise attachment_conflict("resumed appliance console set mismatched")
    address_attrs = {"type", "controller", "bus", "port"}
    for address in console.findall("address"):
        if set(address.attrib) - address_attrs:
            raise attachment_conflict("resumed appliance console set mismatched")


def _validate_rng(devices: ET.Element) -> None:
    """Require exactly the reference ``<rng model="virtio">`` backed by ``/dev/urandom``."""
    nodes = devices.findall("rng")
    if len(nodes) != 1 or nodes[0].attrib != {"model": "virtio"}:
        raise attachment_conflict("resumed appliance entropy device mismatched")
    rng = nodes[0]
    if nodes[0].text and nodes[0].text.strip():
        raise attachment_conflict("resumed appliance entropy device mismatched")
    backends = rng.findall("backend")
    if (
        len(backends) != 1
        or backends[0].attrib != {"model": "random"}
        or list(backends[0])
        or (backends[0].text or "").strip() != "/dev/urandom"
    ):
        raise attachment_conflict("resumed appliance entropy device mismatched")
    for tag, attributes in _RNG_CHILD_ATTRS.items():
        nodes_by_tag = rng.findall(tag)
        if len(nodes_by_tag) > 1 or any(
            list(node) or set(node.attrib) - attributes for node in nodes_by_tag
        ):
            raise attachment_conflict("resumed appliance entropy device mismatched")
    if any(child.tag not in {"backend", *_RNG_CHILD_ATTRS} for child in rng):
        raise attachment_conflict("resumed appliance entropy device mismatched")


_RNG_CHILD_ATTRS = {
    "alias": {"name"},
    "address": {"type", "domain", "bus", "slot", "function"},
}


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
        raise attachment_conflict("resumed appliance normalized device mismatched")
    if any(child.tag not in child_allowlists[device.tag] or list(child) for child in device):
        raise attachment_conflict("resumed appliance normalized device mismatched")
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
        raise attachment_conflict("resumed appliance normalized device mismatched")
    child_counts = {tag: len(device.findall(tag)) for tag in child_allowlists[device.tag]}
    if any(count > 1 for count in child_counts.values()):
        raise attachment_conflict("resumed appliance normalized device mismatched")
    if device.tag == "controller" and device.get("type") not in {
        "pci",
        "usb",
        "sata",
        "virtio-serial",
    }:
        raise attachment_conflict("resumed appliance normalized device mismatched")
    if device.tag == "input" and (
        device.get("type") not in {"mouse", "keyboard", "tablet"}
        or device.get("bus") not in {"ps2", "usb", "virtio"}
    ):
        raise attachment_conflict("resumed appliance normalized device mismatched")
    if device.tag == "memballoon" and device.get("model") not in {"virtio", "none"}:
        raise attachment_conflict("resumed appliance normalized device mismatched")
    if device.tag == "emulator" and (device.text or "").strip() == "":
        raise attachment_conflict("resumed appliance emulator mismatched")


def validate_appliance_xml(xml: str, expected: ExpectedAttachmentState) -> None:
    """Validate an active appliance definition while tolerating libvirt-owned normalization."""
    try:
        root = parse_libvirt_xml(xml)
    except (ET.ParseError, DefusedXmlException, ValueError) as exc:
        raise attachment_conflict("could not parse resumed appliance definition") from exc
    if root.findtext("name") != expected.appliance.name:
        raise attachment_conflict("resumed appliance name mismatched")
    _validate_appliance(root, True, expected)
