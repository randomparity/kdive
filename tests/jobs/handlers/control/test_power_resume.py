"""The control.power RESUME worker path: paused->ready, failed resume->failed (#1254, ADR-0378)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import LiteralString
from uuid import UUID, uuid4

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, JobState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.domain.operations.jobs import Job, JobKind, PowerAction
from kdive.jobs.handlers.control.control import power_handler
from kdive.jobs.provider_context import take_provider_kind
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.audit import args_digest
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


class _ResumesConcurrently:
    """A control whose ``power`` commits ``paused->ready`` from a side connection first.

    Simulates a redelivery/other worker that wins the race: by the time the handler re-reads
    under the lock, the System is already READY (not PAUSED), so the handler must NOT re-audit.
    """

    def __init__(self, url: str, system_id: UUID) -> None:
        self._url = url
        self._system_id = system_id
        self.calls: list[tuple[str, PowerAction]] = []

    def power(self, domain_name: str, action: PowerAction) -> None:
        self.calls.append((domain_name, action))
        with psycopg.connect(self._url) as conn:
            conn.execute(
                "UPDATE systems SET state = %s WHERE id = %s",
                (SystemState.READY.value, str(self._system_id)),
            )


async def _fetch_audit(pool: AsyncConnectionPool, sid: UUID) -> list[tuple]:
    query: LiteralString = (
        "SELECT tool, object_kind, transition, args_digest FROM audit_log "
        "WHERE object_id = %s ORDER BY ts"
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(query, (sid,))
        return await cur.fetchall()


async def _seed_system(
    pool: AsyncConnectionPool, state: SystemState, *, domain_name: str | None = "kdive-x"
) -> UUID:
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
                domain_name=domain_name,
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
                result = await power_handler(conn, _resume_job(sid), resolver=resolver)
                assert await _sys_state(conn, sid) is SystemState.READY
            # The handler returns the resolved system id and tags the provider kind for metrics.
            assert result == str(sid)
            assert take_provider_kind() == "local-libvirt"
            assert control.calls == [("kdive-x", PowerAction.RESUME)]
            # Exactly one paused->ready audit row, with the exact tool/object/transition/args.
            rows = await _fetch_audit(pool, sid)
            assert rows == [
                (
                    "control.power",
                    "systems",
                    "paused->ready",
                    args_digest({"system_id": str(sid), "action": PowerAction.RESUME.value}),
                )
            ]
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_resume_uses_derived_domain_when_unnamed(migrated_url: str) -> None:
    # A System with no stored domain_name resolves the domain via domain_name_for(system.id);
    # the derived name (not None/some other id) is what the provider resume is driven with.
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.PAUSED, domain_name=None)
            control = _FakeControl()
            resolver = provider_resolver(controller=control)
            async with pool.connection() as conn:
                await power_handler(conn, _resume_job(sid), resolver=resolver)
            assert control.calls == [(domain_name_for(sid), PowerAction.RESUME)]
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_resume_skips_state_and_audit_when_already_ready_after_power(migrated_url: str) -> None:
    # If a concurrent delivery commits paused->ready while control.power runs, the handler's
    # re-read under the lock sees READY (not PAUSED) and must skip the state write AND the audit:
    # the guard is `system is not None AND state is PAUSED`, never a lone existence check.
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.PAUSED)
            control = _ResumesConcurrently(migrated_url, sid)
            resolver = provider_resolver(controller=control)
            async with pool.connection() as conn:
                result = await power_handler(conn, _resume_job(sid), resolver=resolver)
                assert await _sys_state(conn, sid) is SystemState.READY
            assert result == str(sid)
            assert control.calls == [("kdive-x", PowerAction.RESUME)]
            # No second paused->ready audit row: the concurrent delivery already owned it.
            assert await _fetch_audit(pool, sid) == []
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_failed_resume_leaves_paused_for_retry(migrated_url: str) -> None:
    # A control.power fault must NOT condemn a healthy paused guest to FAILED: it re-raises (the
    # job retries) and leaves the System PAUSED — a determinate, recoverable landing.
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
                assert await _sys_state(conn, sid) is SystemState.PAUSED
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_resume_redelivered_after_commit_is_noop(migrated_url: str) -> None:
    # A resume job only exists because admission saw PAUSED; a worker-time READY means a prior
    # delivery already committed paused->ready, so the re-run is an idempotent no-op success — not
    # a terminal refusal that would dead-letter a resume that actually succeeded.
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            control = _FakeControl()
            resolver = provider_resolver(controller=control)
            async with pool.connection() as conn:
                result = await power_handler(conn, _resume_job(sid), resolver=resolver)
                assert result == str(sid)
                assert await _sys_state(conn, sid) is SystemState.READY  # untouched
            assert control.calls == []  # never touched the guest
        finally:
            await pool.close()

    asyncio.run(scenario())
