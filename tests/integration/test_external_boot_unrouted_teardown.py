"""#3017: systems.teardown routes a preparing activation whose activate job allocated no authority.

The activate job's marker is the durable route (migration 0167, ADR-0620 amendment).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.router import route_marked
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.jobs import cancel_job
from kdive.mcp.tools.lifecycle.allocations.lifecycle import release_allocation
from kdive.mcp.tools.lifecycle.runs.steps import boot_run
from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.integration.external_boot_support import (
    CREDENTIAL,
    PreparingProvider,
    configure_external_boot,
    fetch_one,
    register_incarnation,
    seed_public_external_boot,
)
from tests.jobs.handlers.external_boot.seeding import RecordingAcknowledger
from tests.jobs.handlers.external_boot.support import RecordingTeardownExecutor
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver
from tests.support.object_store import INERT_OBJECT_STORE

_MARKER = "external_boot_authority_v1"


async def _boot(pool: AsyncConnectionPool, resolver: Any) -> tuple[str, str]:
    """Create the activation `preparing` through the public Run boot; return System and job."""
    run_id, system_id = await seed_public_external_boot(pool)
    admitted = await boot_run(pool, runs_support.ctx(), run_id, resolver=resolver)
    assert admitted.status == "queued", admitted.model_dump()
    return system_id, admitted.object_id


async def _teardown(pool: AsyncConnectionPool, system_id: str, resolver: Any) -> ToolResponse:
    return await teardown_system(pool, runs_support.ctx(Role.ADMIN), system_id, resolver=resolver)


@pytest.mark.parametrize("activate_job", ["canceled", "queued"])
def test_teardown_routes_by_the_activate_marker(migrated_url: str, activate_job: str) -> None:
    async def body() -> None:
        configure_external_boot()
        resolver = provider_resolver(external_boot=PreparingProvider())
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            system_id, job_id = await _boot(pool, resolver)
            if activate_job == "canceled":
                canceled = await cancel_job(pool, runs_support.ctx(), job_id)
                assert canceled.status == "canceled", canceled.model_dump()
            response = await _teardown(pool, system_id, resolver)
            assert response.status == "queued", response.model_dump()
            async with pool.connection() as conn:
                markers = await fetch_one(
                    conn,
                    "SELECT a.payload -> %s AS activate, t.payload -> %s AS teardown "
                    "FROM jobs a, jobs t WHERE a.id = %s AND t.id = %s",
                    (_MARKER, _MARKER, UUID(job_id), UUID(response.object_id)),
                )
                reservation = await fetch_one(
                    conn,
                    "SELECT r.state FROM external_boot_reservations r "
                    "JOIN external_boot_activations e ON e.id = r.activation_id "
                    "WHERE e.system_id = %s",
                    (UUID(system_id),),
                )

        route = ("provider_kind", "authority_instance", "activation_id")
        teardown = markers["teardown"]
        assert {key: teardown[key] for key in route} == {
            key: markers["activate"][key] for key in route
        }
        assert (teardown["purpose"], teardown["operation"]) == ("teardown", "teardown")
        # Enqueueing credits nothing: only the authority's teardown receipt ends the reservation.
        assert reservation == {"state": "pending"}

    asyncio.run(body())


def _worker(
    pool: AsyncConnectionPool,
    resolver: Any,
    authority_dsn: str,
    teardown_executor: RecordingTeardownExecutor,
    worker_id: str,
) -> Worker:
    """A real worker whose marked jobs reach only the external-boot operations registry."""

    async def must_not_run(_conn: AsyncConnection, job: Job) -> str:
        raise AssertionError(f"a marked {job.kind.value} job reached the ordinary handler")

    operations = build_operations(
        ExternalBootHandlerPorts(
            resolver=resolver,
            incarnation_credential=CREDENTIAL,
            secret_registry=SecretRegistry(),
            acknowledger=RecordingAcknowledger(authority_dsn),
            teardown_executor=teardown_executor,
            artifact_store=INERT_OBJECT_STORE,
        )
    )
    registry = HandlerRegistry()
    registry.register(JobKind.TEARDOWN, route_marked(operations, must_not_run))
    return Worker(
        pool,
        registry,
        worker_id=worker_id,
        incarnation_credential=CREDENTIAL,
        secret_registry=SecretRegistry(),
    )


_OUTCOME_SQL = (
    "SELECT e.state, s.state AS system_state, "
    "(SELECT count(*) FROM external_boot_reservations r WHERE r.activation_id = e.id) "
    "  AS reservations, "
    "(SELECT count(*) FROM external_boot_reservation_releases r WHERE r.activation_id = e.id) "
    "  AS releases, "
    "(SELECT array_agg(a.purpose ORDER BY a.generation) FROM external_boot_authorities a "
    "  WHERE a.activation_id = e.id) AS authorities "
    "FROM external_boot_activations e JOIN systems s ON s.id = e.system_id "
    "WHERE e.system_id = %s"
)


def test_routed_teardown_completes_and_releases(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def body() -> None:
        configure_external_boot()
        provider = PreparingProvider()
        resolver = provider_resolver(external_boot=provider)
        worker_id = "local:unrouted-teardown"
        async with (
            AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool,
            await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed,
        ):
            await register_incarnation(pool, worker_id)
            executor = RecordingTeardownExecutor(seed)
            worker = _worker(
                pool, resolver, authority_role_dsns("kdive_provider_authority"), executor, worker_id
            )
            system_id, job_id = await _boot(pool, resolver)
            canceled = await cancel_job(pool, runs_support.ctx(), job_id)
            assert canceled.status == "canceled", canceled.model_dump()
            response = await _teardown(pool, system_id, resolver)
            assert response.status == "queued", response.model_dump()
            lane = await fetch_one(
                seed, "SELECT dispatch_lane FROM jobs WHERE id = %s", (UUID(response.object_id),)
            )
            claimed = await worker.run_once(lane["dispatch_lane"])
            assert claimed is not None and str(claimed.id) == response.object_id
            outcome = await fetch_one(seed, _OUTCOME_SQL, (UUID(system_id),))
            teardown_job = await fetch_one(
                seed, "SELECT state FROM jobs WHERE id = %s", (UUID(response.object_id),)
            )
            allocation_id = (
                await fetch_one(
                    seed, "SELECT allocation_id FROM systems WHERE id = %s", (UUID(system_id),)
                )
            )["allocation_id"]
            released = await release_allocation(pool, runs_support.ctx(), str(allocation_id))

        assert teardown_job == {"state": "succeeded"}
        assert len(executor.calls) == 1
        assert outcome == {
            "state": "torn_down",
            "system_state": "torn_down",
            "reservations": 0,
            "releases": 0,
            "authorities": ["teardown"],
        }
        assert provider.phases == []
        assert released.status == "released", released.model_dump()

    asyncio.run(body())


async def _seed_authority(conn: AsyncConnection, job_id: str, state: str) -> None:
    """Persist an activate authority row for the job's activation, as the allocator would."""
    worker = f"worker-{uuid4()}"
    await conn.execute(
        "INSERT INTO worker_incarnations "
        "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
        "VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
        (worker, b"1" * 32),
    )
    await conn.execute(
        "INSERT INTO external_boot_authorities "
        "(system_id, allocation_id, activation_id, run_id, plan_identity, job_id, job_attempt, "
        "purpose, provider_kind, authority_instance, worker_incarnation, operation, "
        "operation_identity, operation_digest, generation, state, superseded_at) "
        "SELECT e.system_id, s.allocation_id, e.id, e.run_id, e.plan_identity, j.id, 1, "
        "'activate', j.payload #>> '{external_boot_authority_v1,provider_kind}', "
        "j.payload #>> '{external_boot_authority_v1,authority_instance}', %s, 'activate', "
        "'seeded-activate', %s, 1, %s, CASE WHEN %s = 'superseded' THEN now() END "
        "FROM jobs j "
        "JOIN external_boot_activations e "
        "  ON e.id = (j.payload #>> '{external_boot_authority_v1,activation_id}')::uuid "
        "JOIN systems s ON s.id = e.system_id WHERE j.id = %s",
        (worker, "sha256:" + "2" * 64, state, state, UUID(job_id)),
    )


@pytest.mark.parametrize("blocker", ["second_activate_job", "allocating", "superseded"])
def test_teardown_still_refuses_an_ambiguous_or_authorized_activation(
    migrated_url: str, blocker: str
) -> None:
    async def body() -> None:
        configure_external_boot()
        resolver = provider_resolver(external_boot=PreparingProvider())
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            system_id, job_id = await _boot(pool, resolver)
            canceled = await cancel_job(pool, runs_support.ctx(), job_id)
            assert canceled.status == "canceled", canceled.model_dump()
            async with pool.connection() as conn:
                if blocker == "second_activate_job":
                    await conn.execute(
                        "INSERT INTO jobs "
                        "(id, kind, payload, state, max_attempts, authorizing, dedup_key) "
                        "SELECT %s, kind, payload, 'canceled', max_attempts, authorizing, %s "
                        "FROM jobs WHERE id = %s",
                        (uuid4(), f"copy-{job_id}", UUID(job_id)),
                    )
                else:
                    await _seed_authority(conn, job_id, blocker)
            response = await _teardown(pool, system_id, resolver)

        assert response.status == "error", response.model_dump()
        assert response.data["reason"] == "external_boot_teardown_authority_unresolved"

    asyncio.run(body())
