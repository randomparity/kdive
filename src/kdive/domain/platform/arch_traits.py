"""Per-architecture VM-provisioning traits, keyed on the profile arch.

The local-libvirt provisioning path was hardcoded for x86: the ``q35`` machine type, the
``ttyS0`` serial console, and an explicitly pinned SSH-NIC PCI slot. Those are the only
platform facts that vary by architecture, so they live here as one table rather than as
scattered ``if arch == …`` branches. A consumer resolves ``arch_traits(profile.arch)`` and
reads the field it needs; adding a new architecture is one row, not four edits.
"""

from __future__ import annotations

from dataclasses import dataclass

from kdive.domain.errors import CategorizedError, ErrorCategory


@dataclass(frozen=True, slots=True)
class ArchTraits:
    """The architecture-varying facts of a provisioned System's domain.

    The arch itself is the ``_TRAITS`` dict key (and the renderer emits ``<os type arch=…>`` from
    ``profile.arch`` directly), so it is not duplicated as a field here.

    Attributes:
        machine: The libvirt ``<os type machine=…>`` value — ``q35`` on x86, ``pseries`` on
            POWER. An explicit ``domain_xml_params["machine"]`` still overrides this default.
        console_device: The serial console device, used both as the ``console=<x>`` kernel
            cmdline token and as ``/dev/<x>`` for the readiness marker. ``ttyS0`` on x86; on
            pseries there is no ``ttyS0`` — the serial console is the hypervisor virtual
            console ``hvc0`` (spapr-vty), so a ``console=ttyS0`` guest never emits the marker.
            ``hvc0`` was live-proven under TCG in #1144 — the ``kdive-ready`` marker was observed
            on ``hvc0``.
        pin_nic_slot: Whether the raw ``-device virtio-net-pci`` SSH NIC must pin an explicit
            PCI address. The q35 PCIe root complex needs it (``addr=0x10``) to avoid colliding
            with libvirt's own auto-assigned slots; the pseries spapr-pci-host-bridge assigns
            addresses itself, so a pinned slot is left off there. ``False`` on pseries was
            live-proven under TCG in #1144 — SSH reached the guest over the unpinned virtio NIC.
        kvm_cpu_mode: The ``<cpu mode=…>`` a **KVM** domain pins (ADR-0340). ``host-passthrough``
            on x86 (ADR-0294: the QEMU default ``qemu64`` is x86-64-v1 but EL9 glibc requires
            x86-64-v2, so a wrong model aborts PID 1); ``host-model`` on pseries. A TCG domain
            emits no ``<cpu>`` (the renderer omits it), so this field applies only under KVM.
        emit_acpi_features: Whether the domain emits the x86 ``<features><acpi/><vmcoreinfo/>``
            block (ADR-0340). ``True`` on x86; ``False`` on pseries, whose fw_cfg/VMCOREINFO
            crash-capture behavior is proven in the kdump sub-issue, not rendered here.
        default_crashkernel: The kdump ``crashkernel=<size>`` reservation applied when a System
            gives no per-install override (ADR-0300/0346). ``256M`` on x86_64; ``512M`` on
            ppc64le, which reserves more (RHEL's ``kdump-utils`` floor is 384M for a 2-4 GB guest,
            512M for 4-16 GB — roughly double x86), so a ppc64le kdump kernel does not OOM before
            makedumpfile runs. An explicit ADR-0300 reservation still wins over this default.
    """

    machine: str
    console_device: str
    pin_nic_slot: bool
    kvm_cpu_mode: str
    emit_acpi_features: bool
    default_crashkernel: str


_TRAITS: dict[str, ArchTraits] = {
    "x86_64": ArchTraits(
        machine="q35",
        console_device="ttyS0",
        pin_nic_slot=True,
        kvm_cpu_mode="host-passthrough",
        emit_acpi_features=True,
        default_crashkernel="256M",
    ),
    "ppc64le": ArchTraits(
        machine="pseries",
        console_device="hvc0",
        pin_nic_slot=False,
        kvm_cpu_mode="host-model",
        emit_acpi_features=False,
        default_crashkernel="512M",
    ),
}

# The arches kdive can provision (one per ``_TRAITS`` row). Local-libvirt discovery filters the
# guest arches it advertises to this set (ADR-0338), so a host that can boot an arch kdive does
# not yet support does not advertise it as schedulable.
SUPPORTED_ARCHES: frozenset[str] = frozenset(_TRAITS)


def arch_traits(arch: str) -> ArchTraits:
    """Resolve the platform traits for a profile architecture.

    Args:
        arch: The profile ``arch`` value (the libvirt ``<os type arch=…>`` string).

    Returns:
        The :class:`ArchTraits` for ``arch``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an unknown architecture. The renderer
            fails fast rather than silently defaulting to x86, which would emit a ``q35`` /
            ``ttyS0`` domain that never boots on the real platform.
    """
    traits = _TRAITS.get(arch)
    if traits is None:
        supported = ", ".join(sorted(_TRAITS))
        raise CategorizedError(
            f"unsupported provisioning architecture {arch!r}; supported: {supported}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return traits


def default_crashkernel_summary() -> str:
    """Render the per-arch kdump ``crashkernel`` defaults for the agent-facing tool text.

    The ``runs.install`` ``crashkernel`` field description is built from this single source, so the
    agent contract cannot drift from the trait table — adding an arch updates the text
    automatically (ADR-0346). Example: ``"256M on x86_64, 512M on ppc64le"`` (arch-sorted for a
    stable rendering).

    Returns:
        A comma-separated ``"<size> on <arch>"`` summary over every supported architecture.
    """
    return ", ".join(f"{_TRAITS[arch].default_crashkernel} on {arch}" for arch in sorted(_TRAITS))
