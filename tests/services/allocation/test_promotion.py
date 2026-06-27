"""Service-level behavior tests for queued allocation promotion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.accounting.cost import Selector
from kdive.domain.accounting.records import Budget, Quota
from kdive.domain.capacity.state import AllocationState, ResourceStatus
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.mcp.auth import RequestContext
from kdive.security.audit import args_digest
from kdive.services.allocation.admission.core import (
    AFFINITY_DENIAL_REASON,
    BUDGET_DENIAL_REASON,
    AdmissionOutcome,
    AllocationRequest,
    admit,
)
from kdive.services.allocation.promotion import (
    _is_budget_terminate,
    promote_pending,
    reap_queue_timeouts,
)

_DT = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _resource(conn: psycopg.AsyncConnection, *, pool: str = "local-libvirt") -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={
                CONCURRENT_ALLOCATION_CAP_KEY: 1,
                "vcpus": 64,
                "memory_mb": 65536,
            },
            pool=pool,
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _quota(conn: psycopg.AsyncConnection, *, limit: str = "1000000") -> None:
    await BUDGETS.upsert(
        conn,
        Budget(project="proj", limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT),
    )
    await QUOTAS.upsert(
        conn,
        Quota(
            project="proj",
            max_concurrent_allocations=1_000_000,
            max_concurrent_systems=1_000_000,
            max_pending_allocations=100,
            updated_at=_DT,
        ),
    )


async def _granted(conn: psycopg.AsyncConnection, resource_id: UUID) -> Allocation:
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource_id,
            state=AllocationState.GRANTED,
        ),
    )


async def _queued(
    conn: psycopg.AsyncConnection,
    resource: Resource,
    *,
    created_offset: timedelta = timedelta(0),
    requested_pool: str | None = None,
) -> UUID:
    by_id = resource.id if requested_pool is None else None
    outcome = await admit(
        conn,
        AllocationRequest(
            ctx=RequestContext(principal="bob", agent_session="bob-sess", projects=("proj",)),
            resource=resource,
            project="proj",
            selector=Selector(vcpus=1, memory_gb=0, cost_class="local"),
            window=1,
            on_capacity="queue",
            disk_gb=10,
            requested_kind=None,
            requested_resource_id=by_id,
            requested_pool=requested_pool,
        ),
    )
    assert outcome.allocation is not None
    alloc_id = outcome.allocation.id
    if created_offset != timedelta(0):
        await conn.execute(
            "UPDATE allocations SET created_at = now() + %s WHERE id = %s",
            (created_offset, alloc_id),
        )
    return alloc_id


async def _state(conn: psycopg.AsyncConnection, alloc_id: UUID) -> str:
    cur = await conn.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _failure_category(conn: psycopg.AsyncConnection, alloc_id: UUID) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT failure_category FROM allocations WHERE id = %s", (alloc_id,))
        row = await cur.fetchone()
    return row[0] if row else None


def test_promote_pending_grants_after_capacity_frees(migrated_url: str) -> None:
    async def _run() -> tuple[int, str, tuple[str, str] | None]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)

            promoted = await promote_pending(conn)
            state = await _state(conn, queued)
            cur = await conn.execute(
                "SELECT principal, agent_session FROM audit_log "
                "WHERE object_id = %s AND transition = 'requested->granted'",
                (queued,),
            )
            audit_row = await cur.fetchone()
        return promoted, state, audit_row

    assert asyncio.run(_run()) == (1, "granted", ("bob", "bob-sess"))


def test_promote_pending_grants_by_pool_to_freed_member(migrated_url: str) -> None:
    async def _run() -> tuple[int, str, str | None]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn, pool="big")
            await _quota(conn)
            holder = await _granted(conn, resource.id)  # fills the single host slot
            queued = await _queued(conn, resource, requested_pool="big")
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)

            promoted = await promote_pending(conn)
            state = await _state(conn, queued)
            cur = await conn.execute("SELECT resource_id FROM allocations WHERE id = %s", (queued,))
            row = await cur.fetchone()
            resource_id = str(row[0]) if row and row[0] is not None else None
        return promoted, state, resource_id

    promoted, state, resource_id = asyncio.run(_run())
    assert promoted == 1
    assert state == "granted"
    assert resource_id is not None  # stamped onto the freed pool member


def test_promote_pending_budget_denial_fails_without_retry(migrated_url: str) -> None:
    async def _run() -> tuple[int, int, str, tuple[str, str] | None, str]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)
            await conn.execute("UPDATE budgets SET limit_kcu = 0 WHERE project = 'proj'")

            first = await promote_pending(conn)
            second = await promote_pending(conn)
            state = await _state(conn, queued)
            cur = await conn.execute(
                "SELECT principal, args_digest FROM audit_log "
                "WHERE object_id = %s AND transition = 'requested->failed'",
                (queued,),
            )
            audit_row = await cur.fetchone()
        expected_digest = args_digest(
            {
                "reason": "budget_exceeded",
                "project": "proj",
                "resource_id": str(resource.id),
            }
        )
        return first, second, state, audit_row, expected_digest

    first, second, state, audit_row, expected_digest = asyncio.run(_run())
    assert (first, second, state) == (0, 0, "failed")
    assert audit_row == ("system:reconciler", expected_digest)


def test_promote_pending_affinity_denial_is_not_budget_failure(migrated_url: str) -> None:
    async def _run() -> tuple[int, str, int]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await conn.execute(
                "UPDATE resources SET owner_project = 'other' WHERE id = %s",
                (resource.id,),
            )
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)

            promoted = await promote_pending(conn)
            state = await _state(conn, queued)
            cur = await conn.execute(
                "SELECT count(*) FROM audit_log "
                "WHERE object_id = %s AND transition = 'requested->failed'",
                (queued,),
            )
            failed_audits = await cur.fetchone()
            assert failed_audits is not None
        return promoted, state, int(failed_audits[0])

    assert asyncio.run(_run()) == (0, "requested", 0)


def test_budget_terminate_requires_budget_reason() -> None:
    affinity_denial = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.ALLOCATION_DENIED,
        reason=AFFINITY_DENIAL_REASON,
        queueable=False,
    )
    budget_denial = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.ALLOCATION_DENIED,
        reason=BUDGET_DENIAL_REASON,
        queueable=False,
    )

    assert not _is_budget_terminate(affinity_denial)
    assert _is_budget_terminate(budget_denial)


def test_reap_queue_timeouts_fails_only_aged_requested_rows(migrated_url: str) -> None:
    async def _run() -> tuple[int, str, str]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            await _granted(conn, resource.id)
            aged = await _queued(conn, resource, created_offset=timedelta(hours=-48))
            young = await _queued(conn, resource, created_offset=timedelta(minutes=-5))

            reaped = await reap_queue_timeouts(conn, timedelta(hours=24))
            aged_state = await _state(conn, aged)
            young_state = await _state(conn, young)
        return reaped, aged_state, young_state

    assert asyncio.run(_run()) == (1, "failed", "requested")


def test_budget_terminate_writes_allocation_denied_failure_category(migrated_url: str) -> None:
    async def _run() -> str | None:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)
            await conn.execute("UPDATE budgets SET limit_kcu = 0 WHERE project = 'proj'")

            await promote_pending(conn)
            return await _failure_category(conn, queued)

    assert asyncio.run(_run()) == "allocation_denied"


def test_reap_queue_timeouts_writes_queue_timeout_failure_category(migrated_url: str) -> None:
    async def _run() -> str | None:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            await _granted(conn, resource.id)
            aged = await _queued(conn, resource, created_offset=timedelta(hours=-48))

            await reap_queue_timeouts(conn, timedelta(hours=24))
            return await _failure_category(conn, aged)

    assert asyncio.run(_run()) == "queue_timeout"
