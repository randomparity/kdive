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
    """Return the boot-deadline multiplier for a System's persisted accelerator (ADR-0341).

    KVM guests run at native speed and are unscaled (``1.0``); the KVM path does not read
    configuration, so an over-optimistic operator value can never break the fast path. Every
    other classification — ``"tcg"`` and, per the TCG-safe fallback, an unknown or ``None``
    (unrecorded) accelerator — scales by the operator-tunable
    ``KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER``. Scaling ``None``/unknown is deliberate: an
    over-optimistic ``kvm`` classification then degrades to a slow-but-correct boot rather
    than a spurious timeout.

    Args:
        accel: The System's persisted accelerator (``"kvm"`` / ``"tcg"`` / ``None``).

    Returns:
        ``1.0`` for KVM, else the configured multiplier (``>= 1.0``).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the multiplier is set but malformed.
    """
    if accel == "kvm":
        return 1.0
    return config.require(LIBVIRT_TCG_DEADLINE_MULTIPLIER)
