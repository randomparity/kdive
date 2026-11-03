"""Authority-side module-volume sweep over the authority's own pool (ADR-0588, ADR-0603)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import cast
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.module_volume_sweep import (
    MAX_OWNED_KEYS,
    ModuleVolumeKeyV1,
    ModuleVolumeKind,
    ModuleVolumeSweepRequestV1,
    ModuleVolumeSweepResponseV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    ModuleVolumeOwner,
)
from kdive.providers.remote_libvirt.reaping.module_volumes import (
    ModuleVolumeReaperConn,
    list_owned_module_volumes,
    reap_orphaned_module_volumes,
)

_SWEEP_SECONDS = 300.0


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
        """List the authority's owned module volumes, or delete the unretained listed ones."""
        if request.phase == "list":
            return await self._list()
        retained = [_owner(item) for item in request.retained]
        listed = [_owner(item) for item in request.candidates]
        deadline = self._clock() + _SWEEP_SECONDS

        def sweep() -> int:
            return reap_orphaned_module_volumes(
                self._connection,
                self._pool_name,
                self._identity_port,
                retained_owners=lambda: retained,
                deadline=deadline,
                clock=self._clock,
                only=listed,
            )

        return ModuleVolumeSweepResponseV1(removed=await self._executor.run(sweep))

    async def _list(self) -> ModuleVolumeSweepResponseV1:
        def listing() -> list[ModuleVolumeKeyV1]:
            return [
                _key(owner)
                for _name, owner in list_owned_module_volumes(self._connection, self._pool_name)
            ]

        owned = await self._executor.run(listing)
        if len(owned) > MAX_OWNED_KEYS:
            raise CategorizedError(
                "the authority pool holds more module volumes than one sweep can name",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"owned": len(owned)},
            )
        return ModuleVolumeSweepResponseV1(removed=0, owned=tuple(owned))


def _owner(item: ModuleVolumeKeyV1) -> ModuleVolumeOwner:
    return ModuleVolumeOwner(str(item.system_id), str(item.run_id), item.operation_nonce, item.kind)


def _key(owner: ModuleVolumeOwner) -> ModuleVolumeKeyV1:
    return ModuleVolumeKeyV1(
        system_id=UUID(owner.system_id),
        run_id=UUID(owner.run_id),
        operation_nonce=owner.operation_nonce,
        kind=cast(ModuleVolumeKind, owner.kind),
    )
