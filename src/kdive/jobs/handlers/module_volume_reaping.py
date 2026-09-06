"""Worker handler for the closed remote module-volume maintenance job."""

import logging
from collections.abc import Sequence
from typing import Protocol

from psycopg import AsyncConnection

from kdive.db.remote_module_attempt_obligations import (
    RemoteModuleAttemptObligationRepository,
    RetainedModuleAttempt,
)
from kdive.domain.operations.jobs import Job
from kdive.jobs.payloads import RemoteModuleVolumeReapPayload, load_payload
from kdive.providers.infra.reaping import ModuleVolumeKey, ModuleVolumeReaper

_MUTATION_KINDS = ("source.ext4", "scratch.ext4")
_REAP_KINDS = ("reaping.journal", "reaped.journal")
_LOG = logging.getLogger(__name__)


class _ObligationReader(Protocol):
    async def retained_owners(self, conn: AsyncConnection) -> Sequence[RetainedModuleAttempt]: ...


def _expand(retained: Sequence[RetainedModuleAttempt]) -> list[ModuleVolumeKey]:
    keys: list[ModuleVolumeKey] = []
    for owner in retained:
        kinds = (
            (*_MUTATION_KINDS, *_REAP_KINDS)
            if owner.mutation_retained and owner.reap_retained
            else _MUTATION_KINDS
            if owner.mutation_retained
            else _REAP_KINDS
            if owner.reap_retained
            else ()
        )
        keys.extend(
            ModuleVolumeKey(
                str(owner.attempt.system_id),
                str(owner.attempt.run_id),
                owner.attempt.operation_nonce,
                kind,
            )
            for kind in kinds
        )
    return keys


async def remote_module_volume_reap_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    reaper: ModuleVolumeReaper,
    _repository: _ObligationReader | None = None,
) -> None:
    """Let the provider request the current retained set after enumerating each host."""
    load_payload(job, RemoteModuleVolumeReapPayload)
    repository = _repository or RemoteModuleAttemptObligationRepository()

    async def retained_owners() -> list[ModuleVolumeKey]:
        return _expand(await repository.retained_owners(conn))

    removed = await reaper.reap_module_volumes(retained_owners)
    _LOG.info("remote module-volume reap completed", extra={"removed": removed})
