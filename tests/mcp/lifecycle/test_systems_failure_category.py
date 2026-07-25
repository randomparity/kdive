"""A `failed` System reports its failing job's real category, not a hard-coded one (#1550).

`systems.get` used to render every `SystemState.FAILED` as `infrastructure_failure`, which
`_RETRYABLE_BY_CATEGORY` turns into `retryable: true` — telling an agent to retry a
non-retryable configuration error. ADR-0454 resolves the category from the job that actually
failed the System, keeping the default only for the reconciler-orphan path that has no job.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import SYSTEM_FAILING_JOB_KINDS, Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import CheckSshReachablePayload, ReprovisionPayload, SystemPayload
from kdive.mcp.tools.lifecycle.systems.view import (
    NO_JOB_SYSTEM_FAILURE_DETAIL,
    SystemsListRequest,
    get_system,
    list_systems,
    system_envelope,
)
from tests.mcp.systems_support import (
    ctx as _ctx,
)
from tests.mcp.systems_support import (
    granted_allocation as _granted_allocation,
)
from tests.mcp.systems_support import (
    pool as _pool,
)
from tests.mcp.systems_support import (
    provider_resolver as _provider_resolver,
)
from tests.mcp.systems_support import (
    provisioning_profile as _provisioning_profile,
)

_DT = datetime(2026, 1, 1, tzinfo=UTC)


# --- unit: the envelope ---------------------------------------------------------------


def _system(state: SystemState = SystemState.FAILED) -> System:
    return System(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        allocation_id=uuid4(),
        state=state,
        provisioning_profile=_provisioning_profile(),
    )


def _failed_job(
    category: ErrorCategory | None,
    failure_context: dict[str, str] | None = None,
    *,
    kind: JobKind = JobKind.PROVISION,
) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=kind,
        payload={"system_id": str(uuid4())},
        state=JobState.FAILED,
        max_attempts=3,
        error_category=category,
        failure_context=failure_context or {},
        authorizing={"principal": "user-1", "agent_session": "s", "project": "proj"},
        dedup_key="dk-1",
    )


def test_failed_system_reports_the_jobs_category_not_infrastructure_failure() -> None:
    # The #1550 regression: a configuration error must not be reported as a retryable
    # infrastructure fault just because the System is `failed`.
    message = (
        "uploaded rootfs checksum is not owned by this System's investigation; "
        "finalize the upload in the investigation this System is bound to"
    )
    job = _failed_job(ErrorCategory.CONFIGURATION_ERROR, {"failure_message": message})

    resp = system_envelope(_system(), failing_job=job)

    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    assert resp.retryable is False
    assert resp.detail == message
    assert resp.data["failing_job_id"] == str(job.id)
    assert resp.data["current_status"] == "failed"


def test_failed_system_without_a_job_keeps_the_infrastructure_default() -> None:
    # The reconciler orphan (`repair_stalled_restoring_systems`) drives a System to `failed`
    # with no job to attribute it to; the default is what that path is for.
    resp = system_envelope(_system(), failing_job=None)

    assert resp.error_category == "infrastructure_failure"
    assert resp.retryable is True
    assert resp.detail == NO_JOB_SYSTEM_FAILURE_DETAIL
    assert resp.detail
    assert "failing_job_id" not in resp.data


def test_failed_system_with_uncategorized_job_falls_back_to_the_default() -> None:
    # A job row whose `error_category` is NULL (never dead-lettered) answers nothing.
    job = _failed_job(None, {"failure_message": "half-written"})

    resp = system_envelope(_system(), failing_job=job)

    assert resp.error_category == "infrastructure_failure"
    assert resp.data["failing_job_id"] == str(job.id)


def test_failed_system_job_without_message_still_links_the_job() -> None:
    job = _failed_job(ErrorCategory.PROVISIONING_FAILURE, {})

    resp = system_envelope(_system(), failing_job=job)

    assert resp.error_category == "provisioning_failure"
    assert resp.detail is None
    assert resp.data["failing_job_id"] == str(job.id)


def test_failed_system_suppresses_the_job_surface_for_a_no_leak_category() -> None:
    # ADR-0123: `data` extras bypass `ToolResponse.failure`'s own suppression, so the
    # job-derived surface is gated on the same no-leak rule.
    job = _failed_job(
        ErrorCategory.NOT_FOUND,
        {"failure_message": "secret-host-name leaked here"},
    )

    resp = system_envelope(_system(), failing_job=job)

    assert resp.error_category == "not_found"
    assert resp.detail == "not found"
    assert "failing_job_id" not in resp.data
    assert "secret-host-name" not in str(resp.model_dump())


def test_non_failed_system_ignores_a_supplied_job() -> None:
    job = _failed_job(ErrorCategory.CONFIGURATION_ERROR, {"failure_message": "irrelevant"})

    resp = system_envelope(_system(SystemState.READY), failing_job=job)

    assert resp.status == "ready"
    assert resp.error_category is None
    assert "failing_job_id" not in resp.data


def test_system_failing_job_kinds_are_the_kinds_that_write_failed() -> None:
    # ADR-0454 §2: exactly the kinds whose handlers write `SystemState.FAILED`. Widening this
    # set without a matching writer would let an unrelated failure be reported as the reason.
    assert set(SYSTEM_FAILING_JOB_KINDS) == {
        JobKind.PROVISION,
        JobKind.REPROVISION,
        JobKind.RESTORE,
    }


# --- integration: the lookup and the read path ----------------------------------------


async def _seed_system(conn_pool: AsyncConnectionPool, alloc_id: str, state: SystemState) -> UUID:
    async with conn_pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=UUID(alloc_id),
                state=state,
                provisioning_profile=_provisioning_profile(),
            ),
        )
    return system.id


def _payload_for(kind: JobKind, system_id: UUID) -> SystemPayload:
    """The payload model ``kind`` validates against, carrying ``system_id`` as its join key."""
    if kind is JobKind.REPROVISION:
        return ReprovisionPayload(system_id=str(system_id), profile_digest="digest-1")
    if kind is JobKind.CHECK_SSH_REACHABLE:
        return CheckSshReachablePayload(system_id=str(system_id))
    return SystemPayload(system_id=str(system_id))


async def _dead_letter(
    conn_pool: AsyncConnectionPool,
    *,
    kind: JobKind,
    system_id: UUID,
    dedup_key: str,
    category: ErrorCategory,
    message: str = "boom",
) -> Job:
    """Enqueue, claim, and dead-letter a job the way the worker does."""
    payload = _payload_for(kind, system_id)
    async with conn_pool.connection() as conn:
        await queue.enqueue(
            conn,
            kind,
            payload,
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            dedup_key,
        )
        claimed = await queue.dequeue(conn, "w1")
        assert claimed is not None
        return await queue.fail(
            conn,
            claimed,
            category,
            terminal=True,
            failure_context={"failure_message": message},
        )


def test_latest_failed_job_for_system_finds_the_dead_lettered_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            job = await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-provision",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            async with conn_pool.connection() as conn:
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is not None
        assert found.id == job.id
        assert found.error_category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


def test_latest_failed_job_for_system_ignores_an_unrelated_kind(migrated_url: str) -> None:
    # A failed `check_ssh_reachable` is routine and never wrote `SystemState.FAILED`; reading
    # it as the reason a System is `failed` would be a confident mis-attribution.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.CHECK_SSH_REACHABLE,
                system_id=system_id,
                dedup_key="dk-ssh",
                category=ErrorCategory.TRANSPORT_FAILURE,
            )
            async with conn_pool.connection() as conn:
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is None

    asyncio.run(_run())


def test_latest_failed_job_for_system_ignores_another_systems_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            mine = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            theirs = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=theirs,
                dedup_key="dk-theirs",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            async with conn_pool.connection() as conn:
                found = await queue.latest_failed_job_for_system(conn, mine)
        assert found is None

    asyncio.run(_run())


def test_latest_failed_job_for_system_ignores_a_non_failed_job(migrated_url: str) -> None:
    # A queued/running job carries no `error_category` — `queue.fail` writes it only on the
    # dead-letter branch — so matching a non-`failed` row would answer with NULL.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            async with conn_pool.connection() as conn:
                await queue.enqueue(
                    conn,
                    JobKind.PROVISION,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    "dk-queued",
                )
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is None

    asyncio.run(_run())


def test_latest_failed_job_for_system_takes_the_newest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-first",
                category=ErrorCategory.PROVISIONING_FAILURE,
            )
            newest = await _dead_letter(
                conn_pool,
                kind=JobKind.REPROVISION,
                system_id=system_id,
                dedup_key="dk-second",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            async with conn_pool.connection() as conn:
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is not None
        assert found.id == newest.id

    asyncio.run(_run())


def test_get_system_surfaces_the_provision_jobs_category(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            job = await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-provision",
                category=ErrorCategory.CONFIGURATION_ERROR,
                message="rootfs checksum is not owned by this System's investigation",
            )
            resp = await get_system(
                conn_pool, _ctx(), str(system_id), resolver=_provider_resolver()
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.retryable is False
        assert resp.detail == "rootfs checksum is not owned by this System's investigation"
        assert resp.data["failing_job_id"] == str(job.id)

    asyncio.run(_run())


def test_get_system_falls_back_when_no_job_failed_it(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            resp = await get_system(
                conn_pool, _ctx(), str(system_id), resolver=_provider_resolver()
            )
        assert resp.error_category == "infrastructure_failure"
        assert resp.detail == NO_JOB_SYSTEM_FAILURE_DETAIL
        assert "failing_job_id" not in resp.data

    asyncio.run(_run())


def test_get_system_on_a_ready_system_makes_no_job_lookup(migrated_url: str) -> None:
    # The lookup is on the `failed` branch only; a dead-lettered provision left over from an
    # earlier attempt must not colour a System that is now `ready`.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.READY)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-provision",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            resp = await get_system(
                conn_pool, _ctx(), str(system_id), resolver=_provider_resolver()
            )
        assert resp.status == "ready"
        assert resp.error_category is None

    asyncio.run(_run())


def test_systems_list_keeps_the_flattened_category(migrated_url: str) -> None:
    # ADR-0454 §4, pinned so the disclosed gap is a fact rather than an assumption: the list
    # path shares `system_envelope` and passes no job, so it still reports the default.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-provision",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            resp = await list_systems(
                conn_pool, _ctx(), SystemsListRequest(state=SystemState.FAILED.value)
            )
        assert [item.object_id for item in resp.items] == [str(system_id)]
        assert resp.items[0].error_category == "infrastructure_failure"

    asyncio.run(_run())
