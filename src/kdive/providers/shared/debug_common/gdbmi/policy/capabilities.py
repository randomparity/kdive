"""Debug capabilities a gdbstub target lacks, per architecture and accelerator (ADR-0712)."""

from __future__ import annotations

from enum import StrEnum


class DebugCapability(StrEnum):
    SINGLE_STEP = "single_step"
    HW_WATCHPOINT = "hw_watchpoint"


# Native POWER9 proof (#2739): the pseries gdbstub under KVM neither stops after a single-step
# nor inserts a hardware watchpoint. A new row needs the same kind of native evidence.
_UNSUPPORTED: dict[tuple[str, str], frozenset[DebugCapability]] = {
    ("ppc64le", "kvm"): frozenset({DebugCapability.SINGLE_STEP, DebugCapability.HW_WATCHPOINT}),
}


def supports(arch: str | None, accel: str | None, capability: DebugCapability) -> bool:
    """Return whether a target can do ``capability``; an unknown arch can do everything.

    A NULL accelerator counts as KVM: remote-libvirt records none and renders only KVM domains.
    """
    if arch is None:
        return True
    return capability not in _UNSUPPORTED.get((arch, accel or "kvm"), frozenset())
