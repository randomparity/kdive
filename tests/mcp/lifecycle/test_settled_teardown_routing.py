"""A settled stranded activation's teardown routes to its worker (ADR-0816 amendment, #3551)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import LiteralString
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.capacity.state import (
    ExternalBootActivationState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import Investigation, Run
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs import queue
from kdive.jobs.handlers import systems as systems_handlers
from kdive.jobs.payloads import TeardownPayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools.lifecycle.support._idempotency import record_envelope
from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.mcp.tools.ops.security import breakglass
from kdive.reconciler.repairs.systems import repair_stalled_tearing_down_systems
from kdive.security.authz.rbac import PlatformRole, Role
from tests.mcp import systems_support
from tests.mcp.systems_support import (
    TEST_DT,
    FakeProvisioning,
    ctx,
    granted_allocation,
    provider_resolver,
    seed_system,
)
from tests.services.external_boot.conftest import seed_activation
from tests.support.object_store import INERT_OBJECT_STORE

_ADMIN = RequestContext(
    principal="ops-admin",
    agent_session="sess-admin",
    projects=(),
    roles={},
    platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
)
_MARKED = {"system_id": "", "external_boot_authority_v1": {"activation_id": "a"}}
_SETTLE = (
    "INSERT INTO external_boot_activation_settlements (activation_id, system_id, "
    "authority_instance, authority_composite_state, authority_absent_objects, "
    "authority_recorded_at, worker_domain_name, worker_recorded_at, principal, reason, settled_at) "
    "VALUES (%s, %s, 'authority-a', %s, '{}', now(), 'kdive-d', now(), 'admin', 'stranded', "
    "clock_timestamp())"
)


async def _run_for(pool: AsyncConnectionPool, system_id: str) -> UUID:
    async with pool.connection() as conn:
        investigation = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                title="t",
                state=InvestigationState.ACTIVE,
            ),
        )
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                investigation_id=investigation.id,
                system_id=UUID(system_id),
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=RunState.SUCCEEDED,
                build_profile={},
            ),
        )
    return run.id


async def _system_with_activation(
    pool: AsyncConnectionPool, *, settled: bool, state: SystemState = SystemState.READY
) -> tuple[str, UUID]:
    """A System whose latest activation is abandoned, settled or not."""
    allocation_id = await granted_allocation(pool)
    system_id = await seed_system(pool, allocation_id, state)
    run_id = await _run_for(pool, system_id)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE systems SET domain_name = %s WHERE id = %s", (f"kdive-{system_id}", system_id)
        )
        seeded = await seed_activation(
            conn,
            state=ExternalBootActivationState.ABANDONED,
            cleanup_complete=True,
            system_id=UUID(system_id),
            run_id=run_id,
        )
        if settled:
            await conn.execute(_SETTLE, (seeded.activation.id, system_id, "sha256:" + "e" * 64))
    return system_id, seeded.activation.id


async def _prior_teardown(
    pool: AsyncConnectionPool,
    system_id: str,
    *,
    state: JobState,
    worker_id: str | None = None,
    written: LiteralString = "now()",
) -> UUID:
    """An authority-marked `{system}:teardown` row in ``state``, last written at ``written``."""
    job_id = uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "ALTER TABLE jobs DISABLE TRIGGER jobs_set_updated_at",
        )
        try:
            await conn.execute(
                "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
                "authorizing, dedup_key, updated_at) VALUES (%s, 'teardown', %s, %s, 1, 3, %s, "
                "%s, %s, " + written + ")",
                (
                    job_id,
                    Jsonb({**_MARKED, "system_id": system_id}),
                    state.value,
                    worker_id,
                    Jsonb({"principal": "p", "agent_session": None, "project": "proj"}),
                    f"{system_id}:teardown",
                ),
            )
        finally:
            await conn.execute("ALTER TABLE jobs ENABLE TRIGGER jobs_set_updated_at")
    return job_id


async def _job(pool: AsyncConnectionPool, system_id: str) -> Job:
    async with pool.connection() as conn:
        job = await queue.get_by_dedup_key(conn, f"{system_id}:teardown")
    assert job is not None
    return job


def _is_ordinary(job: Job) -> bool:
    return set(job.payload) == {"system_id"}


Teardown = Callable[[AsyncConnectionPool, str], Awaitable[ToolResponse]]


async def _project_teardown(pool: AsyncConnectionPool, system_id: str) -> ToolResponse:
    return await teardown_system(pool, ctx(Role.ADMIN), system_id)


async def _force_teardown(pool: AsyncConnectionPool, system_id: str) -> ToolResponse:
    return await breakglass.force_teardown(
        pool, _ADMIN, system_id=system_id, reason="stranded #3551"
    )


_TEARDOWNS = pytest.mark.parametrize("teardown", [_project_teardown, _force_teardown])


@_TEARDOWNS
def test_a_settled_system_gets_an_ordinary_teardown_without_the_authority(
    migrated_url: str, teardown: Teardown
) -> None:
    """No resolver is configured, so reaching the authority route would refuse."""

    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            system_id, _ = await _system_with_activation(pool, settled=True)
            response = await teardown(pool, system_id)
            job = await _job(pool, system_id)
        assert response.status == "queued"
        assert response.object_id == str(job.id)
        assert job.kind is JobKind.TEARDOWN and _is_ordinary(job)

    asyncio.run(body())


@_TEARDOWNS
def test_an_unsettled_history_keeps_the_authority_route(
    migrated_url: str, teardown: Teardown
) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            system_id, _ = await _system_with_activation(pool, settled=False)
            response = await teardown(pool, system_id)
        assert response.status == "error"
        assert response.data["reason"] == "external_boot_teardown_authority_unresolved"

    asyncio.run(body())


@_TEARDOWNS
@pytest.mark.parametrize(
    ("state", "worker_id", "written"),
    [
        (JobState.FAILED, None, "now()"),
        (JobState.CANCELED, None, "now()"),
        (JobState.CANCELED, "worker-a", "now() - interval '1 hour'"),
    ],
)
def test_a_finished_authority_row_is_replaced_with_the_ordinary_payload(
    migrated_url: str,
    teardown: Teardown,
    state: JobState,
    worker_id: str | None,
    written: LiteralString,
) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            system_id, _ = await _system_with_activation(pool, settled=True)
            prior = await _prior_teardown(
                pool, system_id, state=state, worker_id=worker_id, written=written
            )
            response = await teardown(pool, system_id)
            job = await _job(pool, system_id)
        assert response.status == "queued"
        assert job.id == prior
        assert job.state is JobState.QUEUED and _is_ordinary(job)
        assert job.worker_id is None

    asyncio.run(body())


@_TEARDOWNS
@pytest.mark.parametrize(
    ("state", "worker_id"),
    [(JobState.RUNNING, "worker-a"), (JobState.QUEUED, None), (JobState.CANCELED, "worker-a")],
)
def test_an_authority_row_that_may_still_run_is_replayed(
    migrated_url: str, teardown: Teardown, state: JobState, worker_id: str | None
) -> None:
    """A canceled row claimed by a worker and written after settlement may still be running."""

    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            system_id, _ = await _system_with_activation(pool, settled=True)
            prior = await _prior_teardown(
                pool,
                system_id,
                state=state,
                worker_id=worker_id,
                written="clock_timestamp() + interval '1 second'",
            )
            response = await teardown(pool, system_id)
            job = await _job(pool, system_id)
        assert response.object_id == str(prior)
        assert job.state is state and not _is_ordinary(job)

    asyncio.run(body())


def _handler(pool: AsyncConnectionPool, system_id: str) -> Awaitable[FakeProvisioning]:
    async def run() -> FakeProvisioning:
        async with pool.connection() as conn:
            job = await queue.enqueue(
                conn,
                JobKind.TEARDOWN,
                TeardownPayload(system_id=system_id),
                {"principal": "system:reconciler", "agent_session": None, "project": "proj"},
                f"{system_id}:teardown",
            )
        provisioner = FakeProvisioning()
        async with pool.connection() as conn:
            await systems_handlers.teardown_handler(
                conn,
                job,
                resolver=provider_resolver(provisioner=provisioner),
                artifact_store=INERT_OBJECT_STORE,
            )
        return provisioner

    return run()


def test_the_worker_tears_down_a_settled_system(migrated_url: str) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            system_id, _ = await _system_with_activation(pool, settled=True)
            provisioner = await _handler(pool, system_id)
            async with pool.connection() as conn:
                system = await SYSTEMS.get(conn, UUID(system_id))
        assert provisioner.torn_down == [f"kdive-{system_id}"]
        assert system is not None and system.state is SystemState.TORN_DOWN

    asyncio.run(body())


def test_a_newer_activation_after_settlement_restores_the_worker_refusal(
    migrated_url: str,
) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            system_id, _ = await _system_with_activation(pool, settled=True)
            async with pool.connection() as conn:
                await seed_activation(
                    conn,
                    state=ExternalBootActivationState.PREPARING,
                    system_id=UUID(system_id),
                    run_id=await _run_for(pool, system_id),
                )
            with pytest.raises(CategorizedError) as refused:
                await _handler(pool, system_id)
        assert refused.value.details["reason"] == "external_boot_teardown_not_supported"

    asyncio.run(body())


@pytest.mark.parametrize("settled", [True, False])
def test_the_stalled_repair_requeues_only_a_settled_system(
    migrated_url: str, settled: bool
) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            system_id, _ = await _system_with_activation(
                pool, settled=settled, state=SystemState.TEARING_DOWN
            )
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, "
                    "dedup_key, updated_at) VALUES ('teardown', %s, 'failed', 3, 3, %s, %s, "
                    "now() - interval '1 hour')",
                    (
                        Jsonb({"system_id": system_id}),
                        Jsonb({"principal": "p", "agent_session": None, "project": "proj"}),
                        f"{system_id}:teardown",
                    ),
                )
                requeued = await repair_stalled_tearing_down_systems(conn)
            job = await _job(pool, system_id)
        assert requeued == (1 if settled else 0)
        assert job.state is (JobState.QUEUED if settled else JobState.FAILED)

    asyncio.run(body())


def test_a_keyed_replacement_is_audited_and_keeps_a_key_that_named_the_old_row(
    migrated_url: str,
) -> None:
    """A key already recorded for the authority row names the same, now ordinary, row."""

    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            system_id, _ = await _system_with_activation(pool, settled=True)
            prior = await _prior_teardown(pool, system_id, state=JobState.FAILED)
            async with pool.connection() as conn:
                old = await queue.get_by_dedup_key(conn, f"{system_id}:teardown")
                assert old is not None
                await record_envelope(
                    conn,
                    principal=ctx(Role.ADMIN).principal,
                    key="settled-retry",
                    project="proj",
                    kind="systems.teardown",
                    envelope=job_envelope(old, "system_id", UUID(system_id)),
                )
            response = await teardown_system(
                pool, ctx(Role.ADMIN), system_id, idempotency_key="settled-retry"
            )
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    "SELECT count(*) FROM audit_log WHERE object_id = %s "
                    "AND transition = 'teardown:recycled'",
                    (system_id,),
                )
                recycled = await cursor.fetchone()
            job = await _job(pool, system_id)
        assert response.object_id == str(prior)
        assert _is_ordinary(job) and job.state is JobState.QUEUED
        assert recycled == (1,)

    asyncio.run(body())
