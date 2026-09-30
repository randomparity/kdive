"""The per-arch and accelerator debug capability table (ADR-0712)."""

from __future__ import annotations

import pytest

from kdive.providers.shared.debug_common.gdbmi.policy.capabilities import (
    DebugCapability,
    supports,
)


@pytest.mark.parametrize("accel", ["kvm", None])
@pytest.mark.parametrize("capability", list(DebugCapability))
def test_ppc64le_kvm_lacks_single_step_and_hw_watchpoints(
    accel: str | None, capability: DebugCapability
) -> None:
    # A NULL accelerator counts as KVM: remote-libvirt records none and renders only KVM.
    assert supports("ppc64le", accel, capability) is False


@pytest.mark.parametrize(
    ("arch", "accel"),
    [("x86_64", "kvm"), ("x86_64", None), ("x86_64", "tcg"), ("ppc64le", "tcg"), (None, "kvm")],
)
@pytest.mark.parametrize("capability", list(DebugCapability))
def test_other_targets_keep_every_capability(
    arch: str | None, accel: str | None, capability: DebugCapability
) -> None:
    assert supports(arch, accel, capability) is True
