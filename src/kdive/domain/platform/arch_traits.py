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
    """Architecture-specific libvirt, console, CPU, and kdump defaults (ADR-0340, ADR-0346).

    The architecture remains the ``_TRAITS`` mapping key rather than a duplicated field. TCG
    rendering ignores ``kvm_cpu_mode``.
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
