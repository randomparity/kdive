"""Local-libvirt provisioning XML rendering."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.platform.arch_traits import ArchTraits, arch_traits
from kdive.profiles.provisioning import ProvisioningProfile, require_concrete_sizing
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    register_kdive_namespace,
    register_qemu_namespace,
)
from kdive.providers.shared.runtime_paths import (
    build_domain_name,
    console_log_path,
    domain_name_for,
)

# loopback-only: local transports never listen off-host (ADR-0210/0218).
_LOOPBACK_HOST = "127.0.0.1"
_PROFILE_POLICY = LocalLibvirtProfilePolicy()
# The bare-fs rootfs roots on the lone virtio disk (`/dev/vda`); the serial `console=` (ttyS0 on
# x86, hvc0 on pseries — see kdive.domain.platform) makes the readiness tail and SSH/drgn path
# observable. kdump's `crashkernel` is the install/boot lane's job, sized against the
# kernel-under-test — never added to the baseline boot (ADR-0272).
_BASELINE_CMDLINE_TEMPLATE = "root=/dev/vda console={console} rw"


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
    accel: str = "kvm",
    emulator: str | None = None,
) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3, ADR-0272).

    A local-libvirt domain is always direct-kernel (the profile validator pairs ``disk-image`` with
    remote-libvirt only), so the ``<os>`` always carries a ``<kernel>`` pointing at the rootfs's own
    baseline kernel (``kernel_path``), an optional ``<initrd>`` (``initrd_path``), and a
    ``<cmdline>`` of ``root=/dev/vda console=<device> rw`` whose serial console is arch-resolved
    (``ttyS0`` on x86, ``hvc0`` on pseries — see ``kdive.domain.platform``). ``kernel_path`` is
    required: a ``None`` is a ``CONFIGURATION_ERROR``, not a silently disk-booting (and so
    non-booting) domain.

    When ``profile.provider.local_libvirt.debug.gdbstub`` is set, a loopback QEMU gdbstub is
    rendered on ``gdb_port`` via the ``<qemu:commandline>`` passthrough (ADR-0210 §1); ``gdb_port``
    is required in that case (the provisioner allocates it) and ignored otherwise. The loopback QEMU
    user-mode SSH port-forward (``-netdev user,...hostfwd=tcp:127.0.0.1:<ssh_port>-:22`` + a
    ``virtio-net`` NIC) is rendered on **every** domain (ADR-0281, #937): the forward is plumbing,
    not a credential, so every ready System is reachable by ``systems.ssh_info`` /
    ``authorize_ssh_key`` without a destructive reprovision. ``ssh_port`` is therefore **required**
    (the provisioner always allocates it), exactly like ``kernel_path``; a ``None`` is a
    ``CONFIGURATION_ERROR``. drgn-live authenticates with the per-System bootstrap key
    (ADR-0289/0315), so the forward exists independently of any profile credential. Both
    passthroughs share **one** ``<qemu:commandline>`` element so a System provisioned for both
    transports renders a single, schema-valid element.

    ``guest_egress`` (ADR-0313, #1031) is the operator-resolved egress policy for the NIC. When
    ``False`` (the default) the forward renders ``restrict=on`` — no guest-initiated egress, the
    ADR-0218 §1 default. When ``True`` it renders ``restrict=off`` so the guest gets normal SLIRP
    NAT + DNS and an agent can install tools at runtime. The flag is operator-owned (resolved from
    ``systems.toml`` at provision, never from the request); the renderer only consumes the resolved
    boolean.

    ``accel``/``emulator`` (ADR-0340) are the resolved accelerator and emulator path for the
    profile arch, from the provisioner's live-capabilities resolution. ``accel`` sets
    ``<domain type>`` (``kvm`` / ``qemu``-TCG) and the ``<cpu>`` element (``host-passthrough``
    x86 / ``host-model`` pseries under KVM; **omitted** under TCG). ``emulator`` is emitted as
    ``<emulator>`` **only** for TCG domains — native KVM relies on libvirt's default binary, so
    an x86_64-under-KVM domain is byte-identical to the pre-ADR-0340 output. The defaults
    ``("kvm", None)`` are exactly that legacy path. A TCG domain requires an ``emulator``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an invalid profile, a gdbstub-flagged
            profile rendered without ``gdb_port``, any domain rendered without ``ssh_port``, or a
            TCG domain (``accel != "kvm"``) rendered without an ``emulator``.
    """
    _ensure_namespaces_registered()
    _PROFILE_POLICY.validate_profile(profile)
    require_concrete_sizing(profile)
    section = profile.provider.local_libvirt
    traits = arch_traits(profile.arch)
    machine = section.domain_xml_params.get("machine", traits.machine)

    domain, devices = _build_baseline_domain(
        system_id,
        profile,
        disk_path=disk_path,
        machine=machine,
        traits=traits,
        accel=accel,
        emulator=emulator,
        kernel_path=kernel_path,
        initrd_path=initrd_path,
    )

    if section.debug.preserve_on_crash:
        _append_preserve_on_crash(domain, devices)
    if section.debug.gdbstub:
        _append_gdbstub(domain, gdb_port)
    # The SSH forward is always rendered (ADR-0281, #937): it is loopback plumbing, not a
    # credential, so every ready System is reachable by `systems.ssh_info`/`authorize_ssh_key`
    # without a destructive reprovision. drgn-live authenticates with the per-System bootstrap key
    # (ADR-0289/0315), independent of the forward's presence.
    _append_ssh_forward(
        domain, ssh_port, guest_egress=guest_egress, pin_nic_slot=traits.pin_nic_slot
    )

    return ET.tostring(domain, encoding="unicode")


def _build_baseline_domain(
    system_id: UUID,
    profile: ProvisioningProfile,
    *,
    disk_path: str,
    machine: str,
    traits: ArchTraits,
    accel: str,
    emulator: str | None,
    kernel_path: Path | None,
    initrd_path: Path | None,
) -> tuple[ET.Element, ET.Element]:
    """Build the always-present local-libvirt domain skeleton (ADR-0340).

    ``<domain type>`` is ``kvm`` under KVM and ``qemu`` (TCG) otherwise. The ``<cpu>`` element,
    the x86-only ``<features>`` block, and the TCG ``<emulator>`` are all arch/accel-resolved
    through ``traits`` — see the per-element helpers.
    """
    # Decode the accelerator once: KVM vs TCG drives the domain type, the <cpu> element, and
    # whether <emulator> is emitted (ADR-0340).
    is_kvm = accel == "kvm"
    domain = ET.Element("domain", type="kvm" if is_kvm else "qemu")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    ET.SubElement(domain, "uuid").text = str(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    # <cpu> stays here (after <vcpu>, before <os>) so the x86-KVM domain is byte-identical to
    # the pre-ADR-0340 output; a TCG domain emits nothing and <os> follows <vcpu> directly.
    _append_guest_cpu(domain, is_kvm=is_kvm, kvm_cpu_mode=traits.kvm_cpu_mode)
    os_el = _append_os(domain, arch=profile.arch, machine=machine)
    _append_direct_kernel(os_el, kernel_path, initrd_path, _baseline_cmdline(traits.console_device))
    if traits.emit_acpi_features:
        _append_crash_capture_features(domain)
    devices = ET.SubElement(domain, "devices")
    if not is_kvm:
        _append_emulator(devices, emulator)
    _append_root_disk(devices, disk_path)
    _append_serial_console(devices, system_id)
    _append_metadata(domain, system_id)
    return domain, devices


def _append_guest_cpu(domain: ET.Element, *, is_kvm: bool, kvm_cpu_mode: str) -> None:
    """Pin the guest CPU per arch under KVM; emit nothing for TCG (ADR-0340, ADR-0294, #956).

    Under KVM the mode is the arch-resolved ``kvm_cpu_mode``: ``host-passthrough`` on x86 and
    ``host-model`` on pseries. The x86 case is load-bearing (ADR-0294) — the QEMU/KVM default
    model ``qemu64`` is x86-64-v1 while EL9/RHEL-family glibc requires x86-64-v2, so a wrong
    model makes an EL9 guest's ``ld.so`` abort PID 1 and the domain never reaches userspace.

    A **TCG** domain emits no ``<cpu>``: QEMU's per-machine default is used, and pinning a model
    would couple the domain to specific QEMU versions. Whether that default meets the guest's
    ISA baseline (x86-64-v2 / POWER9) is proven at the #1144 live boot, not asserted here.
    """
    if not is_kvm:
        return
    ET.SubElement(domain, "cpu", mode=kvm_cpu_mode)


def _append_emulator(devices: ET.Element, emulator: str | None) -> None:
    """Emit ``<emulator>`` for a TCG/foreign-arch domain from the discovered path (ADR-0340).

    Native KVM omits it (libvirt's default binary is correct for the host arch); a TCG domain
    needs the discovered ``qemu-system-<arch>`` because libvirt's default
    (``qemu-system-<host-arch>``) cannot run a foreign guest.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no emulator is known — a TCG domain
            cannot boot without a binary. Discovery never advertises a TCG arch without one, so
            this is a defensive guard, not a normal path.
    """
    if emulator is None:
        raise CategorizedError(
            "a TCG (foreign-arch) local-libvirt domain requires a discovered emulator path",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    ET.SubElement(devices, "emulator").text = emulator


def _append_os(domain: ET.Element, *, arch: str, machine: str) -> ET.Element:
    """Append the libvirt OS section without boot artifacts."""
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=arch, machine=machine).text = "hvm"
    return os_el


def _baseline_cmdline(console_device: str) -> str:
    return _BASELINE_CMDLINE_TEMPLATE.format(console=console_device)


def _append_crash_capture_features(domain: ET.Element) -> None:
    """Append ACPI and VMCOREINFO features needed for host_dump capture."""
    features = ET.SubElement(domain, "features")
    # On x86 the guest's qemu_fw_cfg driver locates the fw_cfg device only via ACPI, so the
    # VMCOREINFO note below is written only when ACPI is present; mirror remote (issue #708,
    # ADR-0215).
    ET.SubElement(features, "acpi")
    # QEMU emits the VMCOREINFO note that drgn/crash need to locate the kernel in a host_dump
    # core only when the domain advertises this feature; mirror remote's domain (issue #703).
    ET.SubElement(features, "vmcoreinfo", state="on")


def _append_root_disk(devices: ET.Element, disk_path: str) -> None:
    """Append the rootfs disk as the lone virtio disk."""
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", file=disk_path)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")


def _append_serial_console(devices: ET.Element, system_id: UUID) -> None:
    """Append the serial console and per-System virtlogd log sink."""
    serial = ET.SubElement(devices, "serial", type="pty")
    # append="off" makes virtlogd truncate the serial log on every power-cycle, so the file holds
    # only the current boot and each Run's capture reads it whole (ADR-0258, #836).
    ET.SubElement(serial, "log", file=str(console_log_path(system_id)), append="off")
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")


def _append_metadata(domain: ET.Element, system_id: UUID) -> None:
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{KDIVE_METADATA_NS}}}system").text = str(system_id)


def _append_direct_kernel(
    os_el: ET.Element, kernel_path: Path | None, initrd_path: Path | None, cmdline: str
) -> None:
    """Render the direct-kernel `<os>` body (ADR-0272); a local domain always boots a kernel.

    Built with ElementTree (no string interpolation), so no path can inject XML. ``cmdline`` is
    the arch-resolved baseline (``root=/dev/vda console=<device> rw``).

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
    ET.SubElement(os_el, "cmdline").text = cmdline


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
    domain: ET.Element,
    ssh_port: int | None,
    *,
    guest_egress: bool = False,
    pin_nic_slot: bool = True,
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
    # On q35 (``pin_nic_slot``) pin an explicit PCI slot. Without ``addr=`` QEMU auto-assigns the
    # first free slot (0x1 on the ``pcie.0`` root complex), which collides with a libvirt-managed
    # ``pcie-root-port``: because this NIC is added via raw ``-device`` on the qemu commandline,
    # libvirt's PCI allocator cannot see it and routes its own devices over the same slot, so
    # ``define``/``start`` fails (``slot 1 function 0 not available``). Slot 0x10 sits in the gap
    # between libvirt's low-numbered root-ports and its high-numbered integrated devices
    # (LPC/USB/SATA at 0x1a-0x1f). The pseries spapr-pci-host-bridge assigns addresses itself, so a
    # pinned slot is left off there and QEMU/libvirt allocate.
    device = "virtio-net-pci,netdev=kdivessh"
    if pin_nic_slot:
        device = f"{device},addr=0x10"
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=device)


def render_customization_domain_xml(
    build_id: UUID,
    *,
    arch: str,
    disk_path: str,
    kernel_path: Path,
    initrd_path: Path | None,
    accel: str,
    emulator: str | None,
    memory_mb: int = 2048,
    vcpu: int = 2,
) -> str:
    """Render the transient customization-boot domain XML for a build (ADR-0345, ADR-0340).

    This is a **dedicated minimal renderer**, not an extension of :func:`render_domain_xml`
    (which requires a ``ProvisioningProfile`` + ``ssh_port`` and renders the System SSH forward /
    gdbstub). The build boots this ``kdive-build-<uuid>`` domain once to self-customize, then
    seals; there is no System, no inbound SSH forward, no gdbstub, and no preserve-on-crash.

    The domain is direct-kernel (ADR-0272): ``<os>`` carries the baseline ``<kernel>``, an optional
    ``<initrd>``, and the ``root=/dev/vda console=<device> rw`` cmdline whose console is
    arch-resolved (``ttyS0`` on x86, ``hvc0`` on pseries). ``<on_reboot>destroy</on_reboot>`` stops
    the domain rather than looping if the firstboot script reboots. ``accel``/``emulator``
    (ADR-0340) set ``<domain type>`` (``kvm`` / ``qemu``-TCG), the ``<cpu>`` element (omitted under
    TCG), and the TCG-only ``<emulator>``. The lone NIC is a SLIRP egress NIC (``restrict=off``) so
    the guest can reach its distro mirrors to install packages.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an unknown ``arch``, a missing
            ``kernel_path``, or a TCG domain (``accel != "kvm"``) rendered without an ``emulator``.
    """
    _ensure_namespaces_registered()
    traits = arch_traits(arch)
    is_kvm = accel == "kvm"
    domain = ET.Element("domain", type="kvm" if is_kvm else "qemu")
    ET.SubElement(domain, "name").text = build_domain_name(build_id)
    ET.SubElement(domain, "uuid").text = str(build_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(memory_mb)
    ET.SubElement(domain, "vcpu").text = str(vcpu)
    _append_guest_cpu(domain, is_kvm=is_kvm, kvm_cpu_mode=traits.kvm_cpu_mode)
    os_el = _append_os(domain, arch=arch, machine=traits.machine)
    _append_direct_kernel(os_el, kernel_path, initrd_path, _baseline_cmdline(traits.console_device))
    if traits.emit_acpi_features:
        _append_crash_capture_features(domain)
    ET.SubElement(domain, "on_reboot").text = "destroy"
    devices = ET.SubElement(domain, "devices")
    if not is_kvm:
        _append_emulator(devices, emulator)
    _append_root_disk(devices, disk_path)
    _append_serial_console(devices, build_id)
    _append_egress_nic(domain, pin_nic_slot=traits.pin_nic_slot)
    return ET.tostring(domain, encoding="unicode")


def _append_egress_nic(domain: ET.Element, *, pin_nic_slot: bool) -> None:
    """Append a SLIRP egress NIC for the customization boot (ADR-0345, ADR-0313).

    ``-netdev user,restrict=off`` is QEMU's built-in unprivileged SLIRP user-mode network with NAT
    + DNS so the guest reaches its distro mirrors to install packages; unlike the System forward
    there is **no** ``hostfwd`` (no inbound SSH), no gdbstub, and no preserve-on-crash.

    On q35 (``pin_nic_slot``) the raw ``-device virtio-net-pci`` must pin ``addr=0x10`` — exactly
    as :func:`_append_ssh_forward` — because libvirt's PCI allocator cannot see a NIC added via the
    raw qemu commandline and otherwise routes its own devices over slot 1, failing ``define``/
    ``start``. The pseries spapr-pci-host-bridge assigns addresses itself, so the pin is left off.
    """
    commandline = _qemu_commandline(domain)
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-netdev")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="user,id=kdivebuild,restrict=off")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-device")
    device = "virtio-net-pci,netdev=kdivebuild"
    if pin_nic_slot:
        device = f"{device},addr=0x10"
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=device)
