"""Tests for the per-architecture VM-provisioning traits table."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.platform.arch_traits import arch_traits


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


def test_unknown_arch_is_a_configuration_error_not_a_silent_x86_default() -> None:
    # A silent x86 fallback would render a q35/ttyS0 domain that never boots on the real platform;
    # fail fast instead, and name the arch and the supported set.
    with pytest.raises(CategorizedError) as exc:
        arch_traits("s390x")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "s390x" in str(exc.value)
