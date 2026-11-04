"""An authority activation records the ``boot`` run step the attach gate requires (#3135)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import JobState, RunState, SystemState
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.models import ExternalBootAuthoritySuccessV1, HandlerRegistry
from kdive.jobs.payloads import Authorizing, InstallPayload
from kdive.jobs.worker import Worker
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug.sessions.lifecycle import _attach_preconditions, _succeeded_boot_result
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.integration._seed import seed_granted_allocation, seed_running_run, seed_system

_DIGEST = "sha256:" + "a" * 64


def _result(run_id: str, system_id: UUID, operation: str) -> ExternalBootAuthoritySuccessV1:
    """A success result carrying only the fields the step recorder reads."""
    return ExternalBootAuthoritySuccessV1.model_construct(
        run_id=UUID(run_id),
        system_id=system_id,
        result=SimpleNamespace(operation=operation),
    )


async def _boot_step(pool: AsyncConnectionPool, run_id: str) -> Any:
    async with pool.connection() as conn:
        return await _succeeded_boot_result(conn, UUID(run_id))


async def _commit(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operation: str,
    job_state: JobState,
) -> Any:
    async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
        allocation_id = await seed_granted_allocation(pool)
        system_id = await seed_system(pool, allocation_id, SystemState.READY)
        run_id = await seed_running_run(pool, system_id)
        async with pool.connection() as conn:
            job = await queue.enqueue(
                conn,
                JobKind.INSTALL,
                InstallPayload(run_id=run_id),
                Authorizing(principal="p", agent_session=None, project="a"),
                f"dk-{uuid4()}",
            )
        committed = job.model_copy(update={"state": job_state})

        async def fake_commit(*_a: Any, **_k: Any) -> Job:
            return committed

        monkeypatch.setattr(queue, "complete_external_boot", fake_commit)
        worker = Worker(
            pool,
            HandlerRegistry(),
            worker_id="w1",
            incarnation_credential=SecretStr("x"),
            secret_registry=SecretRegistry(),
        )
        await worker._commit_external_result(job, _result(run_id, UUID(str(system_id)), operation))
        async with pool.connection() as conn:
            run = await RUNS.update_state(conn, UUID(run_id), RunState.SUCCEEDED)
            gate = await _attach_preconditions(conn, run, "drgn-live")
        return await _boot_step(pool, run_id), gate, system_id


def test_committed_activation_records_succeeded_boot_step(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    step, gate, system_id = asyncio.run(
        _commit(migrated_url, monkeypatch, operation="activate", job_state=JobState.SUCCEEDED)
    )
    assert step == {"system_id": str(system_id), "boot_outcome": "ready"}
    assert isinstance(gate, System)


@pytest.mark.parametrize(
    ("operation", "job_state"),
    [
        ("activate", JobState.FAILED),
        ("activate", JobState.QUEUED),
        ("deadline", JobState.RUNNING),
    ],
)
def test_unfinished_activation_records_no_boot_step(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    job_state: JobState,
) -> None:
    step, gate, _system = asyncio.run(
        _commit(migrated_url, monkeypatch, operation=operation, job_state=job_state)
    )
    assert step is None
    assert isinstance(gate, ToolResponse)
    assert gate.data["reason"] == "boot_first"
