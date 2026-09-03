"""Co-located ``KDIVE_LIBVIRT_*`` settings for the local-libvirt provider (ADR-0087).

A dedicated, dependency-light module (the standard library plus :class:`Setting`, and no
provider import) so aggregating it through the manifest never pulls the ``libvirt``
C-extension into a process that does not use the provider. The provider's readers import
these settings and resolve them via ``kdive.config.get``.

That constraint is why :func:`_private_owned_directory` restates the recovery-root guard
from ``lifecycle/boot/external_boot.py`` instead of importing it; the two are held in step
by ``tests/providers/local_libvirt/test_recovery_root_guard.py``, which opens a directory
this module accepts through the real guard.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

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


def _parse_positive_int(raw: str) -> int:
    """Parse a positive integer, rejecting values <= 0.

    Raises ``ValueError`` so the registry surfaces a ``CONFIGURATION_ERROR``.
    """
    value = int(raw)
    if value <= 0:
        raise ValueError(f"must be > 0 (got {value})")
    return value


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

LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S = Setting(
    name="KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S",
    parse=_parse_positive_int,
    default="1800",
    group="local-libvirt",
    processes=_RT,
    help=(
        "Native-KVM base window (seconds) for the customization boot's completion poll. "
        "30 minutes. Foreign (TCG-emulated) guests scale this by "
        "tcg_deadline_multiplier(accel) (ADR-0341). This is a provisional default absorbing "
        "mirror/network fetch variance; a live-proof measurement will re-pin it."
    ),
    suggest="set an integer number of seconds > 0 (default 1800 = 30 min native-KVM base window)",
)

LIBVIRT_BOOT_WINDOW_S = Setting(
    name="KDIVE_LIBVIRT_BOOT_WINDOW_S",
    parse=_parse_positive_int,
    default="900",
    group="local-libvirt",
    processes=_RT,
    help=(
        "Native-KVM base window (seconds) for the regular boot readiness poll — the window "
        "within which the guest must emit the kdive-ready marker after domain start. "
        "Defaults to 900 s (15 min), which accommodates kdump.service arming (the "
        "kdive-ready marker orders After=kdump.service) on slow hosts such as POWER9 and "
        "large first-dracut builds. Foreign (TCG-emulated) guests scale this by "
        "tcg_deadline_multiplier(accel) (ADR-0341). The window is a ceiling, not a fixed "
        "wait — boot returns the instant the marker appears."
    ),
    suggest="set an integer number of seconds > 0 (default 900 = 15 min native-KVM base window)",
)


def _private_owned_directory(raw: str) -> Path:
    """Resolve an absolute path the local recovery stores will accept as their root.

    Restates the conditions ``_require_private_owned_directory`` enforces on every open
    (``lifecycle/boot/external_boot.py``, ADR-0586): a real directory, mode exactly 0700,
    owned by the running euid. It is restated rather than imported because this module stays
    free of provider imports (see the module docstring), so aggregating it through the
    manifest never pulls the ``libvirt`` C-extension into a process that does not use the
    provider. ``tests/providers/local_libvirt/test_recovery_root_guard.py`` holds the two in
    step by opening a directory this accepts through the real guard.

    Uses ``os.lstat`` so a symlink is judged as itself, matching the stores' ``O_NOFOLLOW``.
    Raises ``ValueError`` so the registry surfaces a ``CONFIGURATION_ERROR``.
    """
    value = Path(raw)
    if not value.is_absolute():
        raise ValueError(f"must be an absolute path (got {raw!r})")
    try:
        entry = os.lstat(value)
    except OSError as exc:
        raise ValueError(f"must be an existing directory ({exc.strerror})") from exc
    if stat.S_ISLNK(entry.st_mode):
        raise ValueError("must be a real directory, not a symlink")
    if not stat.S_ISDIR(entry.st_mode):
        raise ValueError("must be a directory")
    mode = stat.S_IMODE(entry.st_mode)
    if mode != 0o700:
        raise ValueError(f"must be mode 0o700 (owner-only); got {mode:#o}")
    euid = os.geteuid()
    if entry.st_uid != euid:
        raise ValueError(
            f"must be owned by the running user; owned by uid {entry.st_uid}, running as uid {euid}"
        )
    return value


# No default and no required_when. Absence must stay rejectable by name through
# ``Registry.require``, which returns a value only when the setting has no default; and
# leaving the setting never-required keeps the dormant external-boot path off, since
# ``Registry.validate`` would otherwise fail every worker host that has not provisioned a
# root yet. A present value is still validated at startup, because ``validate`` parses every
# declared setting whose name is in the environment.
LIBVIRT_RECOVERY_ROOT = Setting(
    name="KDIVE_LIBVIRT_RECOVERY_ROOT",
    parse=_private_owned_directory,
    group="local-libvirt",
    processes=_RT,
    help=(
        "Provider-owned root holding one local external-boot recovery point per activation "
        "(ADR-0586). Must be an existing owner-only directory — mode 0700, owned by the "
        "running worker account — which the recovery stores re-check on every open. It has "
        "no default: an unset root is rejected by name rather than silently assumed, and "
        "leaving it unset keeps the dormant external-boot path off."
    ),
    suggest=(
        "set an absolute path to an existing mode-0700 directory owned by the worker "
        "account; provisioning creates one per slot under "
        "/var/lib/kdive/live-workers/external-boot-recovery"
    ),
)

SETTINGS = [
    LIBVIRT_URI,
    LIBVIRT_ALLOCATION_CAP,
    LIBVIRT_TCG_DEADLINE_MULTIPLIER,
    LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S,
    LIBVIRT_BOOT_WINDOW_S,
    LIBVIRT_RECOVERY_ROOT,
]
