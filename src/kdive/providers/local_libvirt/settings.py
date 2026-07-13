"""Co-located ``KDIVE_LIBVIRT_*`` settings for the local-libvirt provider (ADR-0087).

A dedicated, dependency-light module (imports only :class:`Setting`) so aggregating it
through the manifest never pulls the ``libvirt`` C-extension into a process that does
not use the provider. The provider's readers import these settings and resolve them via
``kdive.config.get``.
"""

from __future__ import annotations

from kdive.config.registry import Setting

_RT = frozenset({"worker", "reconciler"})


def _parse_tcg_multiplier(raw: str) -> float:
    """Parse the TCG deadline multiplier, rejecting a value below 1.0 (ADR-0341).

    A multiplier < 1 would make a TCG (emulated) deadline *tighter* than the KVM baseline,
    which is never intended; ``1.0`` is the operator opt-out ("do not scale even under TCG").
    Raises ``ValueError`` so the registry surfaces a ``CONFIGURATION_ERROR``.
    """
    value = float(raw)
    if value < 1.0:
        raise ValueError(f"must be >= 1.0 (got {value})")
    return value


LIBVIRT_URI = Setting(
    name="KDIVE_LIBVIRT_URI",
    parse=str,
    default="qemu:///system",
    group="local-libvirt",
    processes=_RT,
    help="libvirt connection URI for the local host.",
)
LIBVIRT_ALLOCATION_CAP = Setting(
    name="KDIVE_LIBVIRT_ALLOCATION_CAP",
    parse=str,
    default="1",
    group="local-libvirt",
    processes=_RT,
    help="Per-host concurrent-Allocation cap.",
)

LIBVIRT_TCG_DEADLINE_MULTIPLIER = Setting(
    name="KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER",
    parse=_parse_tcg_multiplier,
    default="10.0",
    group="local-libvirt",
    processes=_RT,
    help=(
        "Multiplier applied to boot-readiness deadlines for non-KVM (TCG-emulated) guests, "
        "keyed off the System's persisted accelerator. KVM guests are unscaled (1.0); TCG "
        "and unknown accelerators scale by this factor. Must be >= 1.0; 1.0 disables scaling."
    ),
    suggest="set a float >= 1.0 (default 10.0); 1.0 disables TCG deadline scaling",
)

SETTINGS = [LIBVIRT_URI, LIBVIRT_ALLOCATION_CAP, LIBVIRT_TCG_DEADLINE_MULTIPLIER]
