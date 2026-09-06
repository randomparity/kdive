"""Durable-obligation module-volume cleanup lane."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection

import kdive.reconciler.cleanup.provider_resources.module_volume_reaping as reaping_enqueue
from kdive.db.remote_module_attempt_obligations import ModuleAttempt, RetainedModuleAttempt
from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.module_volume_reaping import (
    remote_module_volume_reap_handler,
)
from kdive.providers.infra.reaping import ModuleVolumeKey
from kdive.reconciler.cleanup.provider_resources.module_volume_reaping import (
    enqueue_remote_module_volume_reap,
)

SYSTEM = UUID("00000000-0000-0000-0000-000000000001")
RUN = UUID("00000000-0000-0000-0000-000000000002")
ATTEMPT = ModuleAttempt(SYSTEM, RUN, "3" * 32)


@dataclass
class Repository:
    retained: tuple[RetainedModuleAttempt, ...] = ()
    error: Exception | None = None
    calls: int = 0

    async def retained_owners(self, conn: AsyncConnection) -> tuple[RetainedModuleAttempt, ...]:
        del conn
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.retained


class Reaper:
    def __init__(self, *, invoke: bool = True, count: int = 7) -> None:
        self.invoke = invoke
        self.count = count
        self.owners: list[ModuleVolumeKey] | None = None

    async def reap_module_volumes(self, retained_owners: Any) -> int:
        if self.invoke:
            self.owners = list(await retained_owners())
        return self.count


def _job() -> Job:
    return Job.model_validate(
        {
            "id": UUID("00000000-0000-0000-0000-000000000003"),
            "kind": JobKind.REMOTE_MODULE_VOLUME_REAP,
            "payload": {"schema": "remote-module-volume-reap-v1"},
            "state": JobState.RUNNING,
            "max_attempts": 3,
            "authorizing": {
                "principal": "remote-libvirt",
                "agent_session": None,
                "project": "remote-libvirt",
            },
            "dedup_key": "remote-module-volume-reap:v1",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )


def _run(repository: Repository, reaper: Reaper) -> None:
    return asyncio.run(
        remote_module_volume_reap_handler(
            cast("AsyncConnection", object()), _job(), reaper=reaper, _repository=repository
        )
    )


@pytest.mark.parametrize(
    ("mutation", "reap", "kinds"),
    [
        (True, False, ["source.ext4", "scratch.ext4"]),
        (False, True, ["reaping.journal", "reaped.journal"]),
        (
            True,
            True,
            ["source.ext4", "scratch.ext4", "reaping.journal", "reaped.journal"],
        ),
        (False, False, []),
    ],
)
def test_obligations_expand_to_their_exact_volume_kinds(
    mutation: bool, reap: bool, kinds: list[str]
) -> None:
    repository = Repository((RetainedModuleAttempt(ATTEMPT, mutation, reap),))
    reaper = Reaper()
    _run(repository, reaper)
    assert reaper.owners == [
        ModuleVolumeKey(str(SYSTEM), str(RUN), ATTEMPT.operation_nonce, kind) for kind in kinds
    ]


def test_repository_read_is_deferred_until_provider_invokes_callback() -> None:
    repository = Repository()
    _run(repository, Reaper(invoke=False))
    assert repository.calls == 0


def test_provider_count_is_logged_only_in_worker_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="kdive.jobs.handlers.module_volume_reaping")
    assert _run(Repository(), Reaper(count=23)) is None
    assert any(
        record.message == "remote module-volume reap completed"
        and getattr(record, "removed", None) == 23
        for record in caplog.records
    )


def test_repository_failure_propagates() -> None:
    with pytest.raises(RuntimeError, match="database unavailable"):
        _run(Repository(error=RuntimeError("database unavailable")), Reaper())


def test_enqueue_uses_the_closed_internal_maintenance_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def enqueue(*args: object, **kwargs: object) -> tuple[object, bool]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object(), True

    monkeypatch.setattr(reaping_enqueue.queue, "enqueue_with_status", enqueue)
    assert asyncio.run(enqueue_remote_module_volume_reap(cast("AsyncConnection", object()))) is True
    assert captured["args"][1] is JobKind.REMOTE_MODULE_VOLUME_REAP
    assert captured["kwargs"]["recycle"].value == "terminal_or_canceled"
