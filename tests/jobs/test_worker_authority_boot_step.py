"""An authority activation records the ``boot`` run step the attach gate requires (#3135)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.repositories import JOBS, RUNS
from kdive.domain.capacity.state import JobState
from kdive.domain.lifecycle.records import System
from kdive.jobs import queue
from kdive.jobs import worker as worker_module
from kdive.jobs.models import ExternalBootAuthoritySuccessV1, HandlerRegistry
from kdive.jobs.worker import Worker
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug.sessions.lifecycle import _attach_preconditions, _succeeded_boot_result
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.jobs.handlers.external_boot.seeding import seed_case
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle


def _result(vehicle: Vehicle, operation: str) -> ExternalBootAuthoritySuccessV1:
    """A success result carrying only the fields the step recorder reads."""
    return ExternalBootAuthoritySuccessV1.model_construct(
        run_id=vehicle.run_id,
        system_id=vehicle.system_id,
        activation_id=vehicle.activation_id,
        result=SimpleNamespace(operation=operation),
    )


async def _commit(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operation: str = "activate",
    job_state: JobState = JobState.SUCCEEDED,
    activation_state: str = "active",
) -> tuple[Any, Any, Vehicle]:
    """Drive the worker's commit path; return the recorded step, the attach gate, the vehicle."""
    vehicle = build_vehicle()
    async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
        case = await seed_case(seed, vehicle, purpose="activate", activation_state=activation_state)
        job = await JOBS.get(seed, case.job_id)
    assert job is not None
    committed = job.model_copy(update={"state": job_state})

    async def fake_commit(*_a: Any, **_k: Any) -> Any:
        return committed

    monkeypatch.setattr(queue, "complete_external_boot", fake_commit)
    async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
        worker = Worker(
            pool,
            HandlerRegistry(),
            worker_id="w1",
            incarnation_credential=SecretStr("x"),
            secret_registry=SecretRegistry(),
        )
        await worker._commit_external_result(job, _result(vehicle, operation))
        async with pool.connection() as conn:
            step = await _succeeded_boot_result(conn, vehicle.run_id)
            run = await RUNS.get(conn, vehicle.run_id)
            assert run is not None
            gate = await _attach_preconditions(conn, run, "drgn-live")
    return step, gate, vehicle


def test_committed_activation_records_succeeded_boot_step(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    step, gate, vehicle = asyncio.run(_commit(migrated_url, monkeypatch))
    assert step == {"system_id": str(vehicle.system_id), "boot_outcome": "ready"}
    assert isinstance(gate, System)


@pytest.mark.parametrize(
    ("operation", "job_state", "activation_state"),
    [
        ("activate", JobState.FAILED, "active"),
        ("activate", JobState.QUEUED, "active"),
        ("deadline", JobState.RUNNING, "active"),
        ("activate", JobState.SUCCEEDED, "recovered"),
    ],
    ids=["failed", "queued", "deadline", "superseded-activation"],
)
def test_unfinished_or_superseded_activation_records_no_boot_step(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    job_state: JobState,
    activation_state: str,
) -> None:
    step, gate, _vehicle = asyncio.run(
        _commit(
            migrated_url,
            monkeypatch,
            operation=operation,
            job_state=job_state,
            activation_state=activation_state,
        )
    )
    assert step is None
    assert isinstance(gate, ToolResponse)
    assert gate.data["reason"] == "boot_first"


def test_transient_step_write_failure_is_retried(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = worker_module._activation_is_active
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return await real(*args, **kwargs)

    monkeypatch.setattr(worker_module, "_activation_is_active", flaky)
    monkeypatch.setattr(worker_module, "_BOOT_STEP_RETRY_DELAY_S", 0.0)
    step, _gate, vehicle = asyncio.run(_commit(migrated_url, monkeypatch))
    assert calls == 2
    assert step == {"system_id": str(vehicle.system_id), "boot_outcome": "ready"}


def test_exhausted_step_write_logs_and_leaves_no_step(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(worker_module, "_activation_is_active", broken)
    monkeypatch.setattr(worker_module, "_BOOT_STEP_RETRY_DELAY_S", 0.0)
    step, _gate, _vehicle = asyncio.run(_commit(migrated_url, monkeypatch))
    assert step is None
    assert "boot step was not recorded" in caplog.text


def test_existing_step_for_another_system_is_left_unchanged(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = worker_module._activation_is_active

    async def seed_foreign_step(conn: Any, activation_id: Any, run_id: Any, system_id: Any) -> Any:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'boot', 'succeeded', %s)",
            (run_id, Jsonb({"system_id": "other", "boot_outcome": "ready"})),
        )
        return await real(conn, activation_id, run_id, system_id)

    monkeypatch.setattr(worker_module, "_activation_is_active", seed_foreign_step)
    step, _gate, _vehicle = asyncio.run(_commit(migrated_url, monkeypatch))
    assert step == {"system_id": "other", "boot_outcome": "ready"}
    assert "already has a boot step" in caplog.text
