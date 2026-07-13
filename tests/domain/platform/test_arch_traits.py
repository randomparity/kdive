"""Tests for the per-architecture VM-provisioning traits table."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.platform.arch_traits import _TRAITS, SUPPORTED_ARCHES, arch_traits


def test_x86_64_traits_are_q35_ttys0_pinned() -> None:
    traits = arch_traits("x86_64")
    assert traits.machine == "q35"
    assert traits.console_device == "ttyS0"
    assert traits.pin_nic_slot is True


def test_ppc64le_traits_are_pseries_hvc0_unpinned() -> None:
    # pseries has no ttyS0 (serial console is hvc0) and its spapr-pci-host-bridge assigns PCI
    # addresses, so the SSH NIC must not pin a slot.
    traits = arch_traits("ppc64le")
    assert traits.machine == "pseries"
    assert traits.console_device == "hvc0"
    assert traits.pin_nic_slot is False


def test_x86_64_kvm_cpu_mode_and_acpi_features() -> None:
    # x86 KVM keeps host-passthrough (ADR-0294: qemu64 is x86-64-v1 and EL9 needs v2) and the
    # ACPI/VMCOREINFO features are an x86 firmware assumption (ADR-0340).
    traits = arch_traits("x86_64")
    assert traits.kvm_cpu_mode == "host-passthrough"
    assert traits.emit_acpi_features is True


def test_ppc64le_kvm_cpu_mode_and_no_acpi_features() -> None:
    # pseries KVM uses host-model, and the x86 ACPI/VMCOREINFO block is not rendered — pseries
    # crash-capture is proven in the kdump sub-issue (#1149), not guessed here (ADR-0340).
    traits = arch_traits("ppc64le")
    assert traits.kvm_cpu_mode == "host-model"
    assert traits.emit_acpi_features is False


def test_supported_arches_is_the_traits_keys() -> None:
    # Discovery filters advertised guest arches to this set; it must stay in lockstep with the
    # provisioning table so adding an arch is one _TRAITS row, and it is exactly the two arches
    # kdive provisions today (a drift guard for a future addition).
    assert frozenset(_TRAITS) == SUPPORTED_ARCHES
    assert {"x86_64", "ppc64le"} == SUPPORTED_ARCHES


def test_unknown_arch_is_a_configuration_error_not_a_silent_x86_default() -> None:
    # A silent x86 fallback would render a q35/ttyS0 domain that never boots on the real platform;
    # fail fast instead, and name the arch and the supported set.
    with pytest.raises(CategorizedError) as exc:
        arch_traits("s390x")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "s390x" in str(exc.value)
