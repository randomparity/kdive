"""Accelerator-keyed deadline scaling for the local-libvirt provider (ADR-0341).

TCG (software-emulated, foreign-arch) guests execute an order of magnitude slower than
KVM-accelerated ones, so boot-readiness deadlines tuned for KVM time out spuriously under
TCG. This module holds the single multiplier the provider applies where a guest-execution
deadline is computed, keyed off the System's persisted ``accel`` fact (#1141), so the policy
lives in one place rather than as scattered per-step constants.
"""

from __future__ import annotations

import kdive.config as config
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER


def tcg_deadline_multiplier(accel: str | None) -> float:
    """Return 1 for KVM and the safe TCG multiplier for all other accelerators (ADR-0341)."""
    if accel == "kvm":
        return 1.0
    return config.require(LIBVIRT_TCG_DEADLINE_MULTIPLIER)
