"""Local-libvirt provisioning XML rendering."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile, require_concrete_sizing
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    register_kdive_namespace,
    register_qemu_namespace,
)
from kdive.providers.shared.runtime_paths import console_log_path, domain_name_for

_DEFAULT_MACHINE = "q35"
_GDBSTUB_HOST = "127.0.0.1"  # loopback-only: the local gdbstub never listens off-host (ADR-0210)
_PROFILE_POLICY = LocalLibvirtProfilePolicy()


def _ensure_namespaces_registered() -> None:
    """Register the kdive + qemu XML prefixes when rendering domain XML."""
    # ElementTree keeps namespace prefixes in process-global state. Keep that mutation out of
    # import time and perform it at the rendering boundary that needs deterministic prefixes.
    register_kdive_namespace()
    register_qemu_namespace()


def render_domain_xml(
    system_id: UUID,
    profile: ProvisioningProfile,
    *,
    disk_path: str,
    gdb_port: int | None = None,
) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3).

    When ``profile.provider.local_libvirt.debug.gdbstub`` is set, a loopback QEMU gdbstub is
    rendered on ``gdb_port`` via the ``<qemu:commandline>`` passthrough (ADR-0210 §1); ``gdb_port``
    is required in that case (the provisioner allocates it) and ignored otherwise.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an invalid profile or a gdbstub-flagged
            profile rendered without ``gdb_port``.
    """
    _ensure_namespaces_registered()
    _PROFILE_POLICY.validate_profile(profile)
    require_concrete_sizing(profile)
    section = profile.provider.local_libvirt
    machine = section.domain_xml_params.get("machine", _DEFAULT_MACHINE)

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    ET.SubElement(domain, "uuid").text = str(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", file=disk_path)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    serial = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial, "log", file=str(console_log_path(system_id)))
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{KDIVE_METADATA_NS}}}system").text = str(system_id)

    if section.debug.gdbstub:
        _append_gdbstub(domain, gdb_port)

    return ET.tostring(domain, encoding="unicode")


def _append_gdbstub(domain: ET.Element, gdb_port: int | None) -> None:
    """Append the loopback ``-gdb tcp:127.0.0.1:<port>`` QEMU passthrough (ADR-0210 §1)."""
    if gdb_port is None:
        raise CategorizedError(
            "a gdbstub-provisioned System requires an allocated gdbstub port",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    commandline = ET.SubElement(domain, f"{{{QEMU_NS}}}commandline")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-gdb")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=f"tcp:{_GDBSTUB_HOST}:{gdb_port}")
