"""Local-libvirt provisioning XML rendering."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
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
# The bare-fs rootfs roots on the lone virtio disk (`/dev/vda`); `console=ttyS0` makes the readiness
# tail and SSH/drgn path observable. kdump's `crashkernel` is the install/boot lane's job, sized
# against the kernel-under-test — never added to the baseline boot (ADR-0272).
_BASELINE_CMDLINE = "root=/dev/vda console=ttyS0 rw"


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
    kernel_path: Path | None = None,
    initrd_path: Path | None = None,
    guest_egress: bool = False,
) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3, ADR-0272).

    A local-libvirt domain is always direct-kernel (the profile validator pairs ``disk-image`` with
    remote-libvirt only), so the ``<os>`` always carries a ``<kernel>`` pointing at the rootfs's own
    baseline kernel (``kernel_path``), an optional ``<initrd>`` (``initrd_path``), and a fixed
    ``<cmdline>`` of ``root=/dev/vda console=ttyS0 rw``. ``kernel_path`` is required: a ``None`` is
    a ``CONFIGURATION_ERROR``, not a silently disk-booting (and so non-booting) domain.

    When ``profile.provider.local_libvirt.debug.gdbstub`` is set, a loopback QEMU gdbstub is
    rendered on ``gdb_port`` via the ``<qemu:commandline>`` passthrough (ADR-0210 §1); ``gdb_port``
    is required in that case (the provisioner allocates it) and ignored otherwise. The loopback QEMU
    user-mode SSH port-forward (``-netdev user,...hostfwd=tcp:127.0.0.1:<ssh_port>-:22`` + a
    ``virtio-net`` NIC) is rendered on **every** domain (ADR-0281, #937): the forward is plumbing,
    not a credential, so every ready System is reachable by ``systems.ssh_info`` /
    ``authorize_ssh_key`` without a destructive reprovision. ``ssh_port`` is therefore **required**
    (the provisioner always allocates it), exactly like ``kernel_path``; a ``None`` is a
    ``CONFIGURATION_ERROR``. ``ssh_credential_ref`` now gates only the drgn-live introspection
    credential, never whether the forward exists. Both passthroughs share **one**
    ``<qemu:commandline>`` element so a System provisioned for both transports renders a single,
    schema-valid element.

    ``guest_egress`` (ADR-0313, #1031) is the operator-resolved egress policy for the NIC. When
    ``False`` (the default) the forward renders ``restrict=on`` — no guest-initiated egress, the
    ADR-0218 §1 default. When ``True`` it renders ``restrict=off`` so the guest gets normal SLIRP
    NAT + DNS and an agent can install tools at runtime. The flag is operator-owned (resolved from
    ``systems.toml`` at provision, never from the request); the renderer only consumes the resolved
    boolean.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an invalid profile, a gdbstub-flagged
            profile rendered without ``gdb_port``, or any domain rendered without ``ssh_port``.
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
    # Pin the guest CPU to the host's (ADR-0294, #956). The QEMU/KVM default model (``qemu64``)
    # is x86-64-v1; EL9/RHEL-family glibc requires x86-64-v2, so an EL9 guest's ``ld.so`` aborts
    # PID 1 ("Fatal glibc error: CPU does not support x86-64-v2") and the domain never reaches
    # userspace — sshd is unreachable over the always-rendered forward (the #956 symptom). Debian's
    # v1 baseline booted regardless, which masked this. host-passthrough gives the guest the host
    # CPU (>= v2 on any modern KVM host) and matches the debug/introspection intent of a local VM.
    ET.SubElement(domain, "cpu", mode="host-passthrough")
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    _append_direct_kernel(os_el, kernel_path, initrd_path)
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
    # append="off" (libvirt's default, pinned explicitly) makes virtlogd truncate the serial log
    # on every power-cycle, so the file holds only the current boot and each Run's capture reads
    # it whole — no cross-boot byte offset (ADR-0258, supersedes ADR-0241's local offset, #836).
    ET.SubElement(serial, "log", file=str(console_log_path(system_id)), append="off")
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{KDIVE_METADATA_NS}}}system").text = str(system_id)

    if section.debug.preserve_on_crash:
        _append_preserve_on_crash(domain, devices)
    if section.debug.gdbstub:
        _append_gdbstub(domain, gdb_port)
    # The SSH forward is always rendered (ADR-0281, #937): it is loopback plumbing, not a
    # credential, so every ready System is reachable by `systems.ssh_info`/`authorize_ssh_key`
    # without a destructive reprovision. `ssh_credential_ref` now gates only the drgn-live
    # introspection credential, not whether the forward exists.
    _append_ssh_forward(domain, ssh_port, guest_egress=guest_egress)

    return ET.tostring(domain, encoding="unicode")


def _append_direct_kernel(
    os_el: ET.Element, kernel_path: Path | None, initrd_path: Path | None
) -> None:
    """Render the direct-kernel `<os>` body (ADR-0272); a local domain always boots a kernel.

    Built with ElementTree (no string interpolation), so no path can inject XML.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no kernel path is supplied — a local-libvirt
            domain must never disk-boot the bootloader-less rootfs.
    """
    if kernel_path is None:
        raise CategorizedError(
            "a local-libvirt direct-kernel domain requires a baseline <kernel> path",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    ET.SubElement(os_el, "kernel").text = str(kernel_path)
    if initrd_path is not None:
        ET.SubElement(os_el, "initrd").text = str(initrd_path)
    ET.SubElement(os_el, "cmdline").text = _BASELINE_CMDLINE


def _append_preserve_on_crash(domain: ET.Element, devices: ET.Element) -> None:
    """Render the pvpanic device + ``<on_crash>preserve</on_crash>`` (ADR-0049 / ADR-0233).

    pvpanic notifies the host on a guest panic; ``preserve`` holds the domain (vCPUs stopped)
    instead of destroying it, so a crashed boot stays inspectable for host_dump capture and the
    #747 live-gdb attach.
    """
    ET.SubElement(devices, "panic", model="pvpanic")
    ET.SubElement(domain, "on_crash").text = "preserve"


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


def _append_ssh_forward(
    domain: ET.Element, ssh_port: int | None, *, guest_egress: bool = False
) -> None:
    """Append a loopback QEMU user-mode SSH port-forward + NIC for drgn-live (ADR-0218 §1).

    ``-netdev user`` is QEMU's built-in unprivileged SLIRP user-mode network (no bridge, no root,
    no daemon); ``hostfwd=tcp:127.0.0.1:<port>-:22`` forwards the loopback-only host port to the
    guest's sshd. The ``virtio-net-pci`` device binds the netdev so the guest sees a single NIC it
    brings up by DHCP.

    ``restrict`` is now an operator policy, not an unconditional block (ADR-0313, #1031). Default
    (``guest_egress=False``) renders ``restrict=on`` — the guest is isolated to the forwarded port,
    all guest-initiated outbound traffic (NAT'd internet/DNS, host-network access) is blocked so an
    agent-supplied kernel cannot use the NIC for egress, and only the inbound ``hostfwd`` SSH
    connection works (ADR-0218 §1 default-deny). When the operator opts a Resource in
    (``guest_egress=True``), it renders ``restrict=off`` so the guest gets normal SLIRP NAT + DNS
    (``10.0.2.3``) and can reach its distro mirrors; the operator's network-zone firewall is then
    the enforcement boundary.
    """
    if ssh_port is None:
        raise CategorizedError(
            "a local-libvirt domain always renders the SSH forward and requires an allocated "
            "SSH port",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    commandline = _qemu_commandline(domain)
    restrict = "off" if guest_egress else "on"
    netdev = f"user,id=kdivessh,restrict={restrict},hostfwd=tcp:{_LOOPBACK_HOST}:{ssh_port}-:22"
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-netdev")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=netdev)
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-device")
    # Pin an explicit PCI slot. Without ``addr=`` QEMU auto-assigns the first free slot (0x1 on
    # the q35 ``pcie.0`` root complex), which collides with a libvirt-managed ``pcie-root-port``:
    # because this NIC is added via raw ``-device`` on the qemu commandline, libvirt's PCI
    # allocator cannot see it and routes its own devices over the same slot, so ``define``/``start``
    # fails (``slot 1 function 0 not available``). Slot 0x10 sits in the gap between libvirt's
    # low-numbered root-ports and its high-numbered integrated devices (LPC/USB/SATA at 0x1a-0x1f).
    ET.SubElement(
        commandline, f"{{{QEMU_NS}}}arg", value="virtio-net-pci,netdev=kdivessh,addr=0x10"
    )
