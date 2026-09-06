"""Durable-obligation remote module-volume cleanup lane (ADR-0588)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from psycopg import AsyncConnection

from kdive.db.remote_module_attempt_obligations import (
    RemoteModuleAttemptObligationRepository,
    RetainedModuleAttempt,
)
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, RemoteModuleVolumeReapPayload, load_payload
from kdive.providers.infra.reaping import ModuleVolumeKey, ModuleVolumeReaper

_MUTATION_KINDS = ("source.ext4", "scratch.ext4")
_REAP_KINDS = ("reaping.journal", "reaped.journal")


class _ObligationReader(Protocol):
    async def retained_owners(self, conn: AsyncConnection) -> Sequence[RetainedModuleAttempt]: ...


def _expand(retained: Sequence[RetainedModuleAttempt]) -> list[ModuleVolumeKey]:
    keys: list[ModuleVolumeKey] = []
    for owner in retained:
        attempt = owner.attempt
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
                str(attempt.system_id), str(attempt.run_id), attempt.operation_nonce, kind
            )
            for kind in kinds
        )
    return keys


_AUTHORIZE = Authorizing(principal="remote-libvirt", project="remote-libvirt")
_DEDUP_KEY = "remote-module-volume-reap:v1"
_PAYLOAD = RemoteModuleVolumeReapPayload(schema="remote-module-volume-reap-v1")


async def enqueue_remote_module_volume_reap(conn: AsyncConnection) -> bool:
    """Ensure the one platform-internal worker sweep is queued or recycled."""
    _, admitted = await queue.enqueue_with_status(
        conn,
        JobKind.REMOTE_MODULE_VOLUME_REAP,
        _PAYLOAD,
        _AUTHORIZE,
        _DEDUP_KEY,
        recycle=queue.JobRecyclePolicy.TERMINAL_OR_CANCELED,
    )
    return admitted


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

    await reaper.reap_module_volumes(retained_owners)
