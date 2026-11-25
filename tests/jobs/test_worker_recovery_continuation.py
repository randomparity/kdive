"""Authenticated checkpoints continue through the real worker and Postgres (ADR-0845, ADR-0853)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.repositories import JOBS
from kdive.domain.capacity.state import JobState
from kdive.domain.external_boot_values import OpaqueProviderRef
from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.router import route_marked
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker
from kdive.jobs.worker_telemetry import WorkerTelemetry
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.jobs.handlers.external_boot.conftest import resolver_for
from tests.jobs.handlers.external_boot.seeding import RecordingAcknowledger, seed_case
from tests.jobs.handlers.external_boot.support import RecordingExecutor
from tests.jobs.handlers.external_boot.vehicle import build_vehicle


@pytest.mark.parametrize("operation", ["activate", "recover", "resolve-conflict"])
def test_checkpoint_continues_in_same_worker_claim(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    operation: str,
) -> None:
    async def run() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose=operation,
                operation=operation,
                activation_state={
                    "activate": "prepared",
                    "recover": "active",
                    "resolve-conflict": "recovery_conflict",
                }[operation],
                attempt_state="conflict" if operation == "resolve-conflict" else "recovering",
                with_pre_recovery=operation != "activate",
            )
            job = await JOBS.get(seed, case.job_id)
        assert job is not None

        class Executor(RecordingExecutor):
            async def execute(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
                async with await psycopg.AsyncConnection.connect(
                    migrated_url,
                    autocommit=True,
                ) as observer:
                    current = await JOBS.get(observer, job.id)
                    if operation == "activate":
                        row = await (
                            await observer.execute(
                                "SELECT state,activation_readiness_deadline "
                                "FROM external_boot_activations WHERE id=%s",
                                (vehicle.activation_id,),
                            )
                        ).fetchone()
                        assert row is not None and row[0] == "activating" and row[1] is not None
                    else:
                        row = await (
                            await observer.execute(
                                "SELECT a.state,r.state FROM external_boot_activations a "
                                "JOIN external_boot_recovery_attempts r "
                                "ON r.attempt_id=a.current_attempt_id WHERE a.id=%s",
                                (vehicle.activation_id,),
                            )
                        ).fetchone()
                        assert row == ("recovering", "recovering")
                assert current is not None and current.state is JobState.RUNNING
                assert (current.id, current.attempt, current.worker_id) == (
                    job.id,
                    job.attempt,
                    job.worker_id,
                )
                if operation == "activate":
                    vehicle.port.activate(
                        vehicle.recovery_point, OpaqueProviderRef(ref="fixture/activated")
                    )
                return await super().execute(request)

        executor = Executor("target" if operation == "activate" else "source")
        ports = ExternalBootHandlerPorts(
            resolver=resolver_for(vehicle),
            incarnation_credential=SecretStr(case.credential),
            secret_registry=SecretRegistry(),
            acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
            authority_executor=executor,
            clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        )

        async def ordinary(_conn: AsyncConnection, _job: Job) -> None:
            raise AssertionError("ordinary path must not run")

        handler = route_marked(build_operations(ports), ordinary)
        async with AsyncConnectionPool(
            authority_role_dsns("kdive_worker"),
            min_size=1,
            max_size=5,
            open=False,
        ) as pool:
            worker = Worker(
                pool,
                HandlerRegistry(),
                worker_id=case.worker_incarnation,
                incarnation_credential=SecretStr(case.credential),
                secret_registry=SecretRegistry(),
            )
            with WorkerTelemetry.disabled().job_span(job.kind.value) as span:
                await worker._run_handler(job, handler, span)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as observer:
            current = await JOBS.get(observer, job.id)
            activation = await (
                await observer.execute(
                    "SELECT state FROM external_boot_activations WHERE id=%s",
                    (vehicle.activation_id,),
                )
            ).fetchone()
        assert len(executor.requests) == 1
        assert executor.requests[0].operation.value == operation
        assert current is not None and current.state is JobState.SUCCEEDED
        assert (current.id, current.attempt, current.worker_id) == (
            job.id,
            job.attempt,
            job.worker_id,
        )
        assert activation == (("active" if operation == "activate" else "recovered"),)

    asyncio.run(run())
