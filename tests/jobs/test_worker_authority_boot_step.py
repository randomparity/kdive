"""Authority activation publishes the boot step inside its fenced result commit (ADR-0835)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

import kdive.config as config
from kdive.db.repositories import JOBS, RUNS
from kdive.domain.capacity.state import JobState
from kdive.domain.external_boot_values import OpaqueProviderRef
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job
from kdive.jobs.external_boot_models import (
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthoritySuccessV1,
)
from kdive.jobs.handlers.external_boot.lifecycle import activate_handler
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker
from kdive.mcp.tools.debug.sessions.lifecycle import _attach_preconditions, _succeeded_boot_result
from kdive.mcp.tools.lifecycle.runs.steps import boot_run
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import (
    AUTHORITY_INSTANCE,
    RecordingAcknowledger,
    SeededCase,
    seed_case,
)
from tests.jobs.handlers.external_boot.support import RecordingExecutor
from tests.jobs.handlers.external_boot.vehicle import build_vehicle
from tests.mcp.lifecycle.runs_support import LOCAL_PROFILE_POLICY, ctx, mark_authority_owned
from tests.mcp.systems_support import provider_resolver
from tests.support.object_store import INERT_OBJECT_STORE


async def _prepared_activation(
    migrated_url: str, dsns: Callable[[str], str]
) -> tuple[SeededCase, Job, ExternalBootAuthoritySuccessV1]:
    vehicle = build_vehicle()
    # The external provider boundary has already activated the prepared vehicle.
    vehicle.port.activate(vehicle.recovery_point, OpaqueProviderRef(ref="fixture/activated"))
    async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
        case = await seed_case(seed, vehicle, purpose="activate", activation_state="activating")
        job = await JOBS.get(seed, case.job_id)
    assert job is not None
    handler = activate_handler(
        ExternalBootHandlerPorts(
            resolver=resolver_for(vehicle),
            incarnation_credential=SecretStr(case.credential),
            secret_registry=SecretRegistry(),
            acknowledger=RecordingAcknowledger(dsns("kdive_provider_authority")),
            authority_executor=RecordingExecutor("target"),
            artifact_store=INERT_OBJECT_STORE,
            clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        )
    )
    async with await role_connection(dsns("kdive_worker")) as worker:
        result = await handler(
            worker, job, ExternalBootAuthorityMarkerV1.model_validate(case.marker)
        )
    assert isinstance(result, ExternalBootAuthoritySuccessV1)
    return case, job, result


def _worker(
    pool: AsyncConnectionPool, case: SeededCase, *, credential: str | None = None
) -> Worker:
    return Worker(
        pool,
        HandlerRegistry(),
        worker_id=case.worker_incarnation,
        incarnation_credential=SecretStr(case.credential if credential is None else credential),
        secret_registry=SecretRegistry(),
    )


def test_committed_activation_records_succeeded_boot_step(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def run() -> None:
        case, job, result = await _prepared_activation(migrated_url, authority_role_dsns)
        async with AsyncConnectionPool(
            authority_role_dsns("kdive_worker"), min_size=1, max_size=5, open=False
        ) as pool:
            assert await _worker(pool, case)._commit_external_result(job, result) is False
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as observer:
            step = await _succeeded_boot_result(observer, case.vehicle.run_id)
            persisted = await JOBS.get(observer, job.id)
            built_run = await RUNS.get(observer, case.vehicle.run_id)
            assert built_run is not None
            gate = await _attach_preconditions(observer, built_run, "drgn-live")
        assert step == {"system_id": str(case.vehicle.system_id), "boot_outcome": "ready"}
        assert persisted is not None and persisted.state is JobState.SUCCEEDED
        assert isinstance(gate, System)

    asyncio.run(run())


def test_worker_with_wrong_incarnation_cannot_publish_boot_readiness(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def run() -> None:
        case, job, result = await _prepared_activation(migrated_url, authority_role_dsns)
        async with AsyncConnectionPool(
            authority_role_dsns("kdive_worker"), min_size=1, max_size=5, open=False
        ) as pool:
            assert (
                await _worker(pool, case, credential="wrong")._commit_external_result(job, result)
                is False
            )
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as observer:
            assert await _succeeded_boot_result(observer, case.vehicle.run_id) is None
            persisted = await JOBS.get(observer, job.id)
        assert persisted is not None and persisted.state is JobState.RUNNING

    asyncio.run(run())


def test_force_boot_preserves_active_authority_admission_and_readiness(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def run() -> None:
        case, job, result = await _prepared_activation(migrated_url, authority_role_dsns)
        async with AsyncConnectionPool(
            authority_role_dsns("kdive_worker"), min_size=1, max_size=5, open=False
        ) as pool:
            await _worker(pool, case)._commit_external_result(job, result)
        config.load({"KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE": AUTHORITY_INSTANCE})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as admin:
            async with admin.connection() as seed:
                await seed.execute(
                    "UPDATE resources SET name='authority-host' WHERE id=("
                    "SELECT a.resource_id FROM allocations a "
                    "JOIN systems s ON s.allocation_id=a.id "
                    "WHERE s.id=%s)",
                    (case.vehicle.system_id,),
                )
                await seed.execute(
                    "INSERT INTO run_steps (run_id,step,state,result) "
                    "VALUES (%s,'install','succeeded','{}'::jsonb)",
                    (case.vehicle.run_id,),
                )
                before = await (
                    await seed.execute(
                        "SELECT to_jsonb(rs) FROM run_steps rs WHERE run_id=%s AND step='boot'",
                        (case.vehicle.run_id,),
                    )
                ).fetchone()
            await mark_authority_owned(admin, str(case.vehicle.system_id), AUTHORITY_INSTANCE)
            async with AsyncConnectionPool(
                authority_role_dsns("kdive_server"), min_size=1, max_size=2, open=False
            ) as server:
                response = await boot_run(
                    server,
                    ctx(),
                    str(case.vehicle.run_id),
                    force=True,
                    resolver=provider_resolver(profile_policy=LOCAL_PROFILE_POLICY),
                )
            async with admin.connection() as observer:
                after = await (
                    await observer.execute(
                        "SELECT to_jsonb(rs) FROM run_steps rs WHERE run_id=%s AND step='boot'",
                        (case.vehicle.run_id,),
                    )
                ).fetchone()
        assert response.error_category == "conflict"
        assert response.data["reason"] == "external_boot_restricted"
        assert before is not None and isinstance(before[0], dict)
        assert before[0]["result"] == {
            "system_id": str(case.vehicle.system_id),
            "boot_outcome": "ready",
        }
        assert after == before

    asyncio.run(run())
