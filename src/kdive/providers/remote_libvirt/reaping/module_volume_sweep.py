"""Authority-side module-volume sweep over the authority's own pool (ADR-0588, ADR-0603)."""

from __future__ import annotations

import time
from collections.abc import Callable

from kdive.providers.external_boot_authority.module_volume_sweep import (
    ModuleVolumeSweepRequestV1,
    ModuleVolumeSweepResponseV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.reaping.module_volumes import ModuleVolumeReaperConn


class ModuleVolumeSweepService:
    """Sweep unretained module volumes from the authority's private pool."""

    def __init__(
        self,
        connection: ModuleVolumeReaperConn,
        pool_name: str,
        executor: RemoteModulePreparationExecutor,
        identity_port: RemoteDeviceIdentityPort,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connection = connection
        self._pool_name = pool_name
        self._executor = executor
        self._identity_port = identity_port
        self._clock = clock

    async def sweep(self, request: ModuleVolumeSweepRequestV1) -> ModuleVolumeSweepResponseV1:
        raise NotImplementedError
