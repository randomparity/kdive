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
        pin_nic_slot: Whether the raw ``-device virtio-net-pci`` SSH NIC must pin an explicit
            PCI address. The q35 PCIe root complex needs it (``addr=0x10``) to avoid colliding
            with libvirt's own auto-assigned slots; the pseries spapr-pci-host-bridge assigns
            addresses itself, so a pinned slot is left off there.
    """

    machine: str
    console_device: str
    pin_nic_slot: bool


_TRAITS: dict[str, ArchTraits] = {
    "x86_64": ArchTraits(machine="q35", console_device="ttyS0", pin_nic_slot=True),
    "ppc64le": ArchTraits(machine="pseries", console_device="hvc0", pin_nic_slot=False),
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
