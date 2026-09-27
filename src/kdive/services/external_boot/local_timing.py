"""Read server configuration for a local external-boot timing snapshot."""

import kdive.config as config
from kdive.domain.external_boot_timing import LocalExternalBootTimingV1, resolve_local_timing
from kdive.providers.local_libvirt.settings import (
    LIBVIRT_BOOT_WINDOW_S,
    LIBVIRT_TCG_DEADLINE_MULTIPLIER,
)


def local_external_boot_timing(accel: str | None) -> LocalExternalBootTimingV1:
    return resolve_local_timing(
        accel,
        config.require(LIBVIRT_BOOT_WINDOW_S),
        config.require(LIBVIRT_TCG_DEADLINE_MULTIPLIER),
    )
