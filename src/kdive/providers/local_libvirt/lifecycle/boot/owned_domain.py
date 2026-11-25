"""Pure local owned-domain validation and semantics-v2 identity (ADRs 0587, 0654)."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET  # noqa: S405 - element types follow the shared bounded parser
from uuid import UUID

from kdive.domain.platform.arch_traits import rendered_machine_alias
from kdive.providers.shared.libvirt_external_boot import (
    parse_domain_xml,
    parse_projected_domain_xml,
)
from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    backing_store_projection,
    recorded_gdb_port_from_root,
    recorded_ssh_port_from_root,
)
from kdive.providers.shared.runtime_paths import domain_name_for


def parse_owned_xml(
    xml: str,
    system_id: UUID,
    expected_overlay: str,
    *,
    projected: bool = False,
) -> ET.Element:
    parser = parse_projected_domain_xml if projected else parse_domain_xml
    root = parser(xml)
    if root.tag != "domain" or root.findtext("name") != domain_name_for(system_id):
        raise ValueError("domain ownership does not match the operation lease")
    if root.findtext(f"metadata/{{{KDIVE_METADATA_NS}}}system") != str(system_id):
        raise ValueError("domain ownership metadata does not match the operation lease")
    all_disks = root.findall("devices/disk")
    if len(all_disks) > 4096:
        raise ValueError("domain XML exceeds the owned-storage device bound")
    disks = []
    overlay_references = 0
    for disk in all_disks:
        source = disk.find("source")
        driver = disk.find("driver")
        target = disk.find("target")
        if source is not None and source.get("file") == expected_overlay:
            overlay_references += 1
        if (
            disk.get("type") == "file"
            and disk.get("device") == "disk"
            and source is not None
            and source.get("file") == expected_overlay
            and driver is not None
            and driver.get("type") == "qcow2"
            and target is not None
            and target.get("dev") == "vda"
            and target.get("bus") == "virtio"
            and disk.find("readonly") is None
        ):
            disks.append(disk)
    if len(disks) != 1 or overlay_references != 1:
        raise ValueError("domain overlay ownership is absent or ambiguous")
    return root


def _disk_source_is(disk: ET.Element, expected_overlay: str) -> bool:
    source = disk.find("source")
    return source is not None and source.get("file") == expected_overlay


def owned_system_semantic_identity(xml: str, system_id: UUID, expected_overlay: str) -> str:
    """Return the local authority's libvirt-normalization-tolerant System definition identity.

    This intentionally projects only KDIVE-rendered semantics.  Libvirt-generated ids, aliases,
    addresses, controllers, and default devices are excluded; all root, boot, resource, network,
    guest-agent, metadata, and QEMU-argument fields KDIVE controls remain in the digest.
    """
    root = parse_owned_xml(xml, system_id, expected_overlay)
    os_element = root.find("os")
    matching_disks = [
        disk for disk in root.findall("devices/disk") if _disk_source_is(disk, expected_overlay)
    ]
    disk = matching_disks[0]
    source = disk.find("source")
    driver = disk.find("driver")
    target = disk.find("target")
    assert source is not None and driver is not None and target is not None
    qemu_args = tuple(
        argument.get("value")
        for argument in root.findall(
            "./{http://libvirt.org/schemas/domain/qemu/1.0}commandline/"
            "{http://libvirt.org/schemas/domain/qemu/1.0}arg"
        )
    )
    channels = tuple(
        (channel.get("type"), channel_target.get("type"), channel_target.get("name"))
        for channel in root.findall("devices/channel")
        if (channel_target := channel.find("target")) is not None
    )
    serial_log = root.find("devices/serial/log")
    cpu = root.find("cpu")
    features = root.find("features")
    memory = root.find("memory")
    os_type = os_element.find("type") if os_element is not None else None
    interfaces = tuple(
        {
            "type": interface.get("type"),
            "source": None
            if (interface_source := interface.find("source")) is None
            else interface_source.attrib,
            "model": None
            if (interface_model := interface.find("model")) is None
            else interface_model.get("type"),
        }
        for interface in root.findall("devices/interface")
    )
    value = {
        "schema": "kdive-local-authority-domain-semantics-v2",
        "domain_type": root.get("type"),
        "name": root.findtext("name"),
        "uuid": root.findtext("uuid"),
        "memory_bytes": _memory_bytes(memory),
        "vcpu": root.findtext("vcpu"),
        "cpu": _rendered_cpu_projection(cpu),
        "os": None
        if os_element is None
        else {
            "type": None if os_type is None else (os_type.text, _canonical_os_type_attrib(os_type)),
            "kernel": os_element.findtext("kernel"),
            "initrd": os_element.findtext("initrd"),
            "cmdline": os_element.findtext("cmdline"),
        },
        "features": ()
        if features is None
        else tuple(
            (tag, feature.attrib)
            for tag in ("acpi", "vmcoreinfo")
            if (feature := features.find(tag)) is not None
        ),
        "root_disk": {
            "source_file": source.get("file"),
            "driver": (driver.get("name"), driver.get("type")),
            "target": (target.get("dev"), target.get("bus")),
            "readonly": disk.find("readonly") is not None,
            "backing": backing_store_projection(disk),
        },
        "gdb_port": recorded_gdb_port_from_root(root),
        "ssh_port": recorded_ssh_port_from_root(root),
        "qemu_args": qemu_args,
        "interfaces": interfaces,
        "guest_channels": channels,
        "emulator": root.findtext("devices/emulator") if root.get("type") == "qemu" else None,
        "preserve_on_crash": root.findtext("on_crash") == "preserve",
        "serial_log": None
        if serial_log is None
        else (serial_log.get("file"), serial_log.get("append")),
        "system_metadata": root.findtext(f"metadata/{{{KDIVE_METADATA_NS}}}system"),
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return _digest(b"kdive-local-authority-domain-semantics-v2", payload)


def _rendered_cpu_projection(cpu: ET.Element | None) -> tuple[str | None, str | None] | None:
    """Project ``<cpu>`` only when KDIVE rendered it.

    KDIVE renders ``host-passthrough``/``host-model`` (KVM) or a pinned ``custom`` element with
    ``check='partial'``.  A TCG domain gets no rendered ``<cpu>``, but libvirt adds a defaulted
    ``custom`` element (``check='none'``), which must not move the identity.  Any other ``check``
    (``partial``, or libvirt's ``full`` rewrite of a pinned model) stays in the digest.
    """
    if cpu is None or (cpu.get("mode") == "custom" and cpu.get("check") in (None, "none")):
        return None
    return (cpu.get("mode"), cpu.findtext("model"))


def _canonical_os_type_attrib(os_type: ET.Element) -> dict[str, str]:
    """Project ``os/type`` attributes with libvirt's versioned machine mapped to the alias."""
    attrib = dict(os_type.attrib)
    if "machine" in attrib:
        attrib["machine"] = rendered_machine_alias(attrib.get("arch"), attrib["machine"])
    return attrib


def _memory_bytes(memory: ET.Element | None) -> int | None:
    if memory is None:
        return None
    value = memory.text
    units = {
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1_000_000,
        "mib": 1_048_576,
        "gb": 1_000_000_000,
        "gib": 1_073_741_824,
        "tb": 1_000_000_000_000,
        "tib": 1_099_511_627_776,
    }
    try:
        factor = units[memory.get("unit", "KiB").lower()]
        amount = int(value) if value is not None else -1
    except (KeyError, ValueError) as exc:
        raise ValueError("domain memory is not a supported integer quantity") from exc
    if amount < 0:
        raise ValueError("domain memory is not a supported integer quantity")
    return amount * factor


def _digest(prefix: bytes, payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(prefix + b"\0" + payload).hexdigest()
