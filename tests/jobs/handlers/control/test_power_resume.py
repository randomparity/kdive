"""The control.power RESUME worker path: paused->ready, failed resume->failed (#1254, ADR-0378)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, JobState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.domain.operations.jobs import Job, JobKind, PowerAction
from kdive.jobs.handlers.control.control import power_handler
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 7, 17, tzinfo=UTC)


class _FakeControl:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, PowerAction]] = []
        self._error = error

    def power(self, domain_name: str, action: PowerAction) -> None:
        self.calls.append((domain_name, action))
        if self._error is not None:
            raise self._error


async def _seed_system(pool: AsyncConnectionPool, state: SystemState) -> UUID:
    async with pool.connection() as conn:
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="local-libvirt",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=state,
                provisioning_profile={},
                domain_name="kdive-x",
            ),
        )
    return system.id


def _resume_job(system_id: UUID) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.POWER,
        payload={"system_id": str(system_id), "action": "resume"},
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "user-1", "agent_session": None, "project": "proj"},
        dedup_key=f"{system_id}:power:resume:x",
    )


async def _sys_state(conn: AsyncConnection, sid: UUID) -> SystemState:
    row = await SYSTEMS.get(conn, sid)
    assert row is not None
    return row.state


def _pool(url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(url, min_size=1, max_size=2, open=False)


def test_resume_commits_paused_to_ready(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.PAUSED)
            control = _FakeControl()
            resolver = provider_resolver(controller=control)
            async with pool.connection() as conn:
                await power_handler(conn, _resume_job(sid), resolver=resolver)
                assert await _sys_state(conn, sid) is SystemState.READY
            assert control.calls == [("kdive-x", PowerAction.RESUME)]
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_failed_resume_routes_paused_to_failed(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.PAUSED)
            err = CategorizedError("boom", category=ErrorCategory.CONTROL_FAILURE)
            resolver = provider_resolver(controller=_FakeControl(error=err))
            async with pool.connection() as conn:
                raised = False
                try:
                    await power_handler(conn, _resume_job(sid), resolver=resolver)
                except CategorizedError:
                    raised = True
                assert raised
                assert await _sys_state(conn, sid) is SystemState.FAILED
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_resume_refused_from_ready_system(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            resolver = provider_resolver(controller=_FakeControl())
            async with pool.connection() as conn:
                error: CategorizedError | None = None
                try:
                    await power_handler(conn, _resume_job(sid), resolver=resolver)
                except CategorizedError as exc:
                    error = exc
                assert error is not None
                assert error.category is ErrorCategory.CONFIGURATION_ERROR
                assert await _sys_state(conn, sid) is SystemState.READY  # untouched
        finally:
            await pool.close()

    asyncio.run(scenario())
