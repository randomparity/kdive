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
# loopback-only: local transports never listen off-host (ADR-0210/0218).
_LOOPBACK_HOST = "127.0.0.1"
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
    ssh_port: int | None = None,
) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3).

    When ``profile.provider.local_libvirt.debug.gdbstub`` is set, a loopback QEMU gdbstub is
    rendered on ``gdb_port`` via the ``<qemu:commandline>`` passthrough (ADR-0210 §1); ``gdb_port``
    is required in that case (the provisioner allocates it) and ignored otherwise. When
    ``profile.provider.local_libvirt.ssh_credential_ref`` is set, a loopback QEMU user-mode SSH
    port-forward (``-netdev user,...hostfwd=tcp:127.0.0.1:<ssh_port>-:22`` + a ``virtio-net`` NIC)
    is rendered on ``ssh_port`` for the drgn-live transport (ADR-0218 §2); ``ssh_port`` is required
    in that case and ignored otherwise. Both passthroughs share **one** ``<qemu:commandline>``
    element so a System provisioned for both transports renders a single, schema-valid element.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an invalid profile, a gdbstub-flagged
            profile rendered without ``gdb_port``, or a ``ssh_credential_ref``-set profile rendered
            without ``ssh_port``.
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
    features = ET.SubElement(domain, "features")
    # On x86 the guest's qemu_fw_cfg driver locates the fw_cfg device only via ACPI, so the
    # VMCOREINFO note below is written only when ACPI is present; mirror remote (issue #708,
    # ADR-0215).
    ET.SubElement(features, "acpi")
    # QEMU emits the VMCOREINFO note that drgn/crash need to locate the kernel in a host_dump
    # core only when the domain advertises this feature; mirror remote's domain (issue #703).
    ET.SubElement(features, "vmcoreinfo", state="on")
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
    if section.ssh_credential_ref is not None:
        _append_ssh_forward(domain, ssh_port)

    return ET.tostring(domain, encoding="unicode")


def _qemu_commandline(domain: ET.Element) -> ET.Element:
    """Return the domain's lone ``<qemu:commandline>`` element, creating it if absent.

    Both the gdbstub ``-gdb`` arg and the SSH ``-netdev``/``-device`` args append to one element so
    a System provisioned for both transports renders a single, schema-valid passthrough.
    """
    existing = domain.find(f"{{{QEMU_NS}}}commandline")
    if existing is not None:
        return existing
    return ET.SubElement(domain, f"{{{QEMU_NS}}}commandline")


def _append_gdbstub(domain: ET.Element, gdb_port: int | None) -> None:
    """Append the loopback ``-gdb tcp:127.0.0.1:<port>`` QEMU passthrough (ADR-0210 §1)."""
    if gdb_port is None:
        raise CategorizedError(
            "a gdbstub-provisioned System requires an allocated gdbstub port",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    commandline = _qemu_commandline(domain)
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-gdb")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=f"tcp:{_LOOPBACK_HOST}:{gdb_port}")


def _append_ssh_forward(domain: ET.Element, ssh_port: int | None) -> None:
    """Append a loopback QEMU user-mode SSH port-forward + NIC for drgn-live (ADR-0218 §2).

    ``-netdev user`` is QEMU's built-in unprivileged SLIRP user-mode network (no bridge, no root,
    no daemon); ``hostfwd=tcp:127.0.0.1:<port>-:22`` forwards the loopback-only host port to the
    guest's sshd. The ``virtio-net-pci`` device binds the netdev so the guest sees a single NIC it
    brings up by DHCP. ``restrict=on`` isolates the guest to the forwarded port only — it blocks
    all guest-initiated outbound traffic (NAT'd internet/DNS, host-network access) the drgn-live
    control channel never needs, so an agent-supplied kernel cannot use the NIC for egress; the
    inbound ``hostfwd`` SSH connection still works (ADR-0218 §2, defense-in-depth on the new NIC).
    """
    if ssh_port is None:
        raise CategorizedError(
            "a drgn-live System (ssh_credential_ref set) requires an allocated SSH port",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    commandline = _qemu_commandline(domain)
    netdev = f"user,id=kdivessh,restrict=on,hostfwd=tcp:{_LOOPBACK_HOST}:{ssh_port}-:22"
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-netdev")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=netdev)
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-device")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="virtio-net-pci,netdev=kdivessh")
