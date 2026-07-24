"""Remote-libvirt provisioning XML rendering and tolerant host-XML parsing."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from uuid import UUID

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile, require_concrete_sizing
from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    recorded_gdb_port_from_root,
    register_kdive_namespace,
    register_qemu_namespace,
)
from kdive.providers.shared.libvirt_xml import (
    recorded_gdb_port as recorded_gdb_port,  # re-exported facade for remote provisioning + tests
)
from kdive.providers.shared.libvirt_xml import (
    recorded_ssh_port as recorded_ssh_port,  # re-exported facade for remote provisioning + tests
)
from kdive.providers.shared.libvirt_xml import (
    recorded_ssh_port_from_root as recorded_ssh_port_from_root,
)
from kdive.providers.shared.runtime_paths import domain_name_for

_DEFAULT_NETWORK = "default"
_GUEST_AGENT_CHANNEL = "org.qemu.guest_agent.0"


def _ensure_namespaces_registered() -> None:
    """Register XML prefixes at the rendering boundary."""
    register_kdive_namespace()
    register_qemu_namespace()


def overlay_volume_name(system_id: UUID | str) -> str:
    """The per-System overlay volume name in the host's storage pool (ADR-0080 §3)."""
    return f"kdive-{system_id}-overlay.qcow2"


def supplied_base_volume_name(system_id: UUID | str) -> str:
    """The per-System base volume name a supplied qcow2 is staged into (ADR-0440, #1433).

    System-scoped so a supplied base never collides with an operator-staged volume or another
    System's base, and is reclaimable with the lease.
    """
    return f"kdive-{system_id}-base.qcow2"


def render_volume_xml(name: str, *, capacity_bytes: int, backing_path: str) -> str:
    """Render the overlay volume XML: qcow2, backed by the base image volume."""
    volume = ET.Element("volume")
    ET.SubElement(volume, "name").text = name
    ET.SubElement(volume, "capacity").text = str(capacity_bytes)
    target = ET.SubElement(volume, "target")
    ET.SubElement(target, "format", type="qcow2")
    backing = ET.SubElement(volume, "backingStore")
    ET.SubElement(backing, "path").text = backing_path
    ET.SubElement(backing, "format", type="qcow2")
    return ET.tostring(volume, encoding="unicode")


def render_domain_xml(
    system_id: UUID,
    profile: ProvisioningProfile,
    *,
    pool: str,
    volume: str,
    gdb_addr: str,
    gdb_port: int,
    network: str = _DEFAULT_NETWORK,
    machine: str = "pc",
    ssh_addr: str | None = None,
    ssh_port: int | None = None,
) -> str:
    """Render the tagged remote domain XML (ADR-0080 §2/§4).

    When both ``ssh_addr`` and ``ssh_port`` are given, a per-System user-mode SSH forward NIC is
    appended (ADR-0291): ``-netdev user,...restrict=on,hostfwd=tcp:<ssh_addr>:<ssh_port>-:22`` +
    a ``virtio-net-pci`` device. Omitted (either ``None``) → no forward NIC (guest-agent-only).
    """
    _ensure_namespaces_registered()
    if profile.provider.remote_libvirt_section is None:
        raise CategorizedError(
            "provisioning profile has no remote-libvirt provider section",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    require_concrete_sizing(profile)

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    ET.SubElement(domain, "uuid").text = str(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    # Pin a host-model CPU (ADR-0297, #975 — remote parity of ADR-0294/#956). With no <cpu>,
    # QEMU/KVM defaults to ``qemu64`` = x86-64-v1; EL9/RHEL-family glibc requires x86-64-v2, so an
    # EL9 guest's ``ld.so`` aborts PID 1 ("Fatal glibc error: CPU does not support x86-64-v2")
    # before the guest-agent answers — the domain is unreachable. host-model synthesizes a
    # portable, migratable baseline (>= v2 on any modern host) rather than local-libvirt's
    # host-passthrough, because a remote fleet may span heterogeneous hosts.
    ET.SubElement(domain, "cpu", mode="host-model")
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    ET.SubElement(os_el, "boot", dev="hd")
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "vmcoreinfo", state="on")
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="volume", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", pool=pool, volume=volume)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    interface = ET.SubElement(devices, "interface", type="network")
    ET.SubElement(interface, "source", network=network)
    ET.SubElement(interface, "model", type="virtio")
    serial = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")
    channel = ET.SubElement(devices, "channel", type="unix")
    ET.SubElement(channel, "target", type="virtio", name=_GUEST_AGENT_CHANNEL)
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{KDIVE_METADATA_NS}}}system").text = str(system_id)
    commandline = ET.SubElement(domain, f"{{{QEMU_NS}}}commandline")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-gdb")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=f"tcp:{gdb_addr}:{gdb_port}")
    if ssh_addr is not None and ssh_port is not None:
        _append_ssh_forward(commandline, ssh_addr, ssh_port)
    return ET.tostring(domain, encoding="unicode")


def _append_ssh_forward(commandline: ET.Element, ssh_addr: str, ssh_port: int) -> None:
    """Append the per-System user-mode SSH forward NIC to ``<qemu:commandline>`` (ADR-0291).

    ``restrict=on`` isolates the slirp NIC to the inbound forward only (no guest-initiated
    outbound on it); ``hostfwd`` forwards ``ssh_addr:ssh_port`` to the guest sshd on ``:22``.
    Mirrors local-libvirt's loopback forward (ADR-0218), differing only in the routable ACL'd
    bind address. ``addr=0x10`` pins the PCI slot so it does not collide with the disk/bridge/
    agent virtio devices.
    """
    netdev = f"user,id=kdivessh,restrict=on,hostfwd=tcp:{ssh_addr}:{ssh_port}-:22"
    device = "virtio-net-pci,netdev=kdivessh,addr=0x10"
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-netdev")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=netdev)
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-device")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=device)


def _parse_domain_xml_strict(domain_xml: str, *, operation: str, domain: str) -> ET.Element:
    try:
        return _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise CategorizedError(
            "malformed remote-libvirt domain XML",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain, "operation": operation},
        ) from exc


def recorded_gdb_port_strict(domain_xml: str, *, operation: str, domain: str) -> int | None:
    """The gdbstub port a domain's XML records; malformed XML is an infrastructure fault."""
    root = _parse_domain_xml_strict(domain_xml, operation=operation, domain=domain)
    return recorded_gdb_port_from_root(root)


def recorded_ssh_port_strict(domain_xml: str, *, operation: str, domain: str) -> int | None:
    """The SSH hostfwd port a domain's XML records; malformed XML is an infrastructure fault."""
    root = _parse_domain_xml_strict(domain_xml, operation=operation, domain=domain)
    return recorded_ssh_port_from_root(root)


def _agent_channel_connected(root: ET.Element) -> bool:
    target = root.find(f"./devices/channel/target[@name='{_GUEST_AGENT_CHANNEL}']")
    return target is not None and target.get("state") == "connected"


def agent_channel_connected(domain_xml: str) -> bool:
    """Whether the live XML reports the guest-agent channel ``state='connected'``."""
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        return False
    return _agent_channel_connected(root)


def agent_channel_connected_strict(domain_xml: str, *, operation: str, domain: str) -> bool:
    """Whether the guest-agent channel is connected; malformed XML is infrastructure failure."""
    root = _parse_domain_xml_strict(domain_xml, operation=operation, domain=domain)
    return _agent_channel_connected(root)


def disk_pool(domain_xml: str) -> str | None:
    """The storage pool the domain's disk records, or ``None``."""
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        return None
    return _disk_pool(root)


def disk_pool_strict(domain_xml: str, *, operation: str, domain: str) -> str | None:
    """The storage pool the domain's disk records; malformed XML is infrastructure failure."""
    root = _parse_domain_xml_strict(domain_xml, operation=operation, domain=domain)
    return _disk_pool(root)


def _disk_pool(root: ET.Element) -> str | None:
    source = root.find("./devices/disk/source")
    if source is None:
        return None
    return source.get("pool")
