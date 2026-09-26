"""Resolve local external-boot timing without importing concrete provider execution."""

import math
from datetime import timedelta

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_timing import LocalExternalBootTimingV1
from kdive.providers.local_libvirt.settings import (
    LIBVIRT_BOOT_WINDOW_S,
    LIBVIRT_TCG_DEADLINE_MULTIPLIER,
)


def local_external_boot_timing(accel: str | None) -> LocalExternalBootTimingV1:
    """Resolve one bounded local readiness window and containing deadline budget."""
    base = config.require(LIBVIRT_BOOT_WINDOW_S)
    multiplier = 1.0 if accel == "kvm" else config.require(LIBVIRT_TCG_DEADLINE_MULTIPLIER)
    maximum = timedelta.max.days * 86_400 + timedelta.max.seconds
    try:
        window = math.ceil(base * multiplier)
        budget = math.ceil((base + 300) * multiplier)
    except OverflowError, ValueError:
        window = budget = maximum + 1
    if not 0 < window < budget <= maximum:
        raise CategorizedError(
            "local external-boot timing exceeds the supported range; reduce the boot window",
            category=ErrorCategory.CONFIGURATION_ERROR,
            terminal=True,
        )
    return LocalExternalBootTimingV1(
        accel=accel if accel in {"kvm", "tcg"} else None,
        console_window_s=window,
        deadline_budget_s=budget,
    )
