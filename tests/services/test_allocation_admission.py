"""Tests for the per-host capacity check inside M1 admission (ADR-0023, ADR-0007 §5).

These focus on the M0 host-cap behavior `admit` still enforces (count only non-terminal,
ignore terminal, fail closed on a bad cap, serialize on the resource lock). The
budget/quota and reserve/idempotency behavior lives in
``test_admission_budget_quota.py``; here budget + quota are seeded generous so the host
cap is the binding constraint. Real Postgres; injected contexts.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.accounting.cost import Selector
from kdive.domain.accounting.records import Budget, Quota
from kdive.domain.capacity.state import AllocationState, ResourceStatus
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.domain.pcie import PCIE_DEVICES_KEY, PCIeDescriptor
from kdive.mcp.auth import RequestContext
from kdive.security.audit import args_digest
from kdive.services.allocation.admission.core import (
    AllocationRequest,
    admit,
)
from tests.db_waits import wait_until_backend_waiting

_DT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))
SEL = Selector(vcpus=1, memory_gb=0, cost_class="local")


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


def _admit(conn: psycopg.AsyncConnection, resource: Resource, *, project: str = "proj"):  # type: ignore[no-untyped-def]
    ctx = (
        CTX
        if project == "proj"
        else RequestContext(principal="alice", agent_session="s", projects=(project,))
    )
    return admit(
        conn,
        AllocationRequest(ctx=ctx, resource=resource, project=project, selector=SEL, window=1),
    )


async def _seed_budget_quota(conn: psycopg.AsyncConnection, *, project: str = "proj") -> None:
    await BUDGETS.upsert(
        conn,
        Budget(project=project, limit_kcu=Decimal("1000000"), spent_kcu=Decimal(0), updated_at=_DT),
    )
    await QUOTAS.upsert(
        conn,
        Quota(
            project=project,
            max_concurrent_allocations=1_000_000,
            max_concurrent_systems=1_000_000,
            updated_at=_DT,
        ),
    )


async def _seed_resource(
    conn: psycopg.AsyncConnection,
    *,
    cap: object,
    owner_project: str | None = None,
    affinity_allowlist: list[str] | None = None,
    disk_gb: int = 500,
) -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={
                CONCURRENT_ALLOCATION_CAP_KEY: cap,
                "vcpus": 64,
                "memory_mb": 65536,
                "disk_gb": disk_gb,
            },
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
            owner_project=owner_project,
            affinity_allowlist=affinity_allowlist or [],
        ),
    )


async def _seed_allocation(
    conn: psycopg.AsyncConnection, resource_id: UUID, state: AllocationState
) -> Allocation:
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource_id,
            state=state,
        ),
    )


async def _seed_queued(conn: psycopg.AsyncConnection, resource_id: UUID) -> Allocation:
    """Seed a queued `requested` row holding only a queue position (resource_id NULL)."""
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=None,
            state=AllocationState.REQUESTED,
            requested_kind=ResourceKind.LOCAL_LIBVIRT,
        ),
    )


async def _count_allocs(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM allocations")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _count_audit(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_admit_under_cap_grants_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=2)
            await _seed_budget_quota(conn)
            outcome = await _admit(conn, res)
            assert outcome.granted is True
            assert outcome.allocation is not None
            assert outcome.allocation.state is AllocationState.GRANTED
            assert await _count_allocs(conn) == 1
            assert await _count_audit(conn) == 1

    asyncio.run(_run())


_NIC = PCIeDescriptor(
    bdf="0000:3b:00.0", vendor_id="8086", device_id="1572", class_code="020000", label="x710"
)


def test_admit_grant_persists_full_snapshot_and_audit(migrated_url: str) -> None:
    # Pin every field the grant stamps onto the row and the audit event, plus the ~1h lease,
    # so a dropped/blanked snapshot field, wrong audit label, or bad lease arithmetic is caught.
    async def _run() -> tuple[Allocation, tuple[str, str, str, str, str] | None, datetime]:
        async with _conn(migrated_url) as conn:
            res = await RESOURCES.insert(
                conn,
                Resource(
                    id=uuid4(),
                    created_at=_DT,
                    updated_at=_DT,
                    kind=ResourceKind.LOCAL_LIBVIRT,
                    capabilities={
                        CONCURRENT_ALLOCATION_CAP_KEY: 2,
                        "vcpus": 64,
                        "memory_mb": 65536,
                        "disk_gb": 500,
                        PCIE_DEVICES_KEY: [dict(_NIC)],
                    },
                    pool="local-libvirt",
                    cost_class="local",
                    status=ResourceStatus.AVAILABLE,
                    host_uri="qemu:///system",
                ),
            )
            await _seed_budget_quota(conn)
            before = datetime.now(UTC)
            outcome = await admit(
                conn,
                AllocationRequest(
                    ctx=CTX,
                    resource=res,
                    project="proj",
                    selector=SEL,
                    window=1,
                    disk_gb=30,
                    shape="gpu-small",
                    pcie_specs=("8086:1572",),
                ),
            )
            assert outcome.allocation is not None
            cur = await conn.execute(
                "SELECT tool, object_kind, transition, args_digest, project FROM audit_log "
                "WHERE object_id = %s",
                (outcome.allocation.id,),
            )
            audit = await cur.fetchone()
        return outcome.allocation, audit, before

    alloc, audit, before = asyncio.run(_run())
    assert alloc.agent_session == "s"
    assert alloc.requested_disk_gb == 30
    assert alloc.shape == "gpu-small"
    assert [c["vendor_id"] for c in alloc.pcie_claim] == ["8086"]
    assert alloc.lease_expiry is not None
    assert timedelta(minutes=59) <= (alloc.lease_expiry - before) <= timedelta(minutes=61)
    assert audit == (
        "allocations.request",
        "allocations",
        "->granted",
        args_digest({"resource_id": str(alloc.resource_id), "project": "proj"}),
        "proj",
    )


def test_admit_over_disk_ceiling_denies_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=2, disk_gb=50)
            await _seed_budget_quota(conn)
            outcome = await admit(
                conn,
                AllocationRequest(
                    ctx=CTX, resource=res, project="proj", selector=SEL, window=1, disk_gb=60
                ),
            )
            assert outcome.granted is False
            assert outcome.allocation is None
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert outcome.details == {"field": "disk_gb", "requested": "60", "ceiling": "50"}
            assert await _count_allocs(conn) == 0  # no durable row on a rejected request

    asyncio.run(_run())


def test_admit_at_disk_ceiling_grants(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=2, disk_gb=50)
            await _seed_budget_quota(conn)
            outcome = await admit(
                conn,
                AllocationRequest(
                    ctx=CTX, resource=res, project="proj", selector=SEL, window=1, disk_gb=50
                ),
            )
            assert outcome.granted is True  # exactly at the ceiling admits

    asyncio.run(_run())


def test_admit_at_cap_denies_with_no_rows(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget_quota(conn)
            await _seed_allocation(conn, res.id, AllocationState.GRANTED)
            outcome = await _admit(conn, res)
            assert outcome.granted is False
            assert outcome.allocation is None
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED
            assert outcome.reason == "at_capacity"
            assert outcome.in_use == 1 and outcome.cap == 1
            assert await _count_allocs(conn) == 1  # no new row
            assert await _count_audit(conn) == 0  # no audit on denial

    asyncio.run(_run())


def test_admit_ignores_terminal_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget_quota(conn)
            await _seed_allocation(conn, res.id, AllocationState.RELEASED)
            await _seed_allocation(conn, res.id, AllocationState.FAILED)
            outcome = await _admit(conn, res)
            assert outcome.granted is True  # terminal rows do not occupy capacity

    asyncio.run(_run())


def test_admit_counts_only_occupying(migrated_url: str) -> None:
    # The host-cap occupancy predicate is GRANTED/ACTIVE/RELEASING (ADR-0069): a queued
    # `requested` row and the terminal rows do NOT occupy. With three occupying rows and
    # cap=3 the host is exactly full, so admit denies and in_use counts only those three.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=3)
            await _seed_budget_quota(conn)
            for state in (
                AllocationState.REQUESTED,  # queued — excluded from occupancy
                AllocationState.GRANTED,
                AllocationState.ACTIVE,
                AllocationState.RELEASING,
                AllocationState.RELEASED,
                AllocationState.FAILED,
            ):
                await _seed_allocation(conn, res.id, state)
            outcome = await _admit(conn, res)
            assert outcome.granted is False
            assert outcome.in_use == 3  # granted/active/releasing only — requested excluded
            assert outcome.cap == 3

    asyncio.run(_run())


def test_queued_row_does_not_block_a_grant(migrated_url: str) -> None:
    # A queued `requested` row holds no host slot: with cap=1 and one queued row present, a
    # concurrent fresh request still grants (the queued row is excluded from occupancy).
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget_quota(conn)
            await _seed_queued(conn, res.id)
            outcome = await _admit(conn, res)
            assert outcome.granted is True

    asyncio.run(_run())


@pytest.mark.parametrize("cap", [None, "two", -1, True])
def test_admit_bad_cap_fails_closed(migrated_url: str, cap: object) -> None:
    # An invalid host cap fails closed as a denial with category configuration_error;
    # admit catches the resolve error and rolls back, so no row survives.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=cap)
            await _seed_budget_quota(conn)
            outcome = await _admit(conn, res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert outcome.details["resource_id"] == str(res.id)
            assert outcome.details["cap"] == repr(cap)
            assert await _count_allocs(conn) == 0

    asyncio.run(_run())


def test_admit_blocks_behind_a_held_resource_lock(migrated_url: str) -> None:
    # Deterministic proof admit acquires LockScope.RESOURCE: pre-hold it on conn A and
    # assert admit on conn B cannot complete until A releases.
    async def _run() -> None:
        async with (
            _conn(migrated_url) as seed,
            _conn(migrated_url) as a,
            _conn(migrated_url) as b,
        ):
            res = await _seed_resource(seed, cap=1)
            await _seed_budget_quota(seed)
            async with a.transaction(), advisory_xact_lock(a, LockScope.RESOURCE, res.id):
                task = asyncio.ensure_future(_admit(b, res))
                await wait_until_backend_waiting(a, b.info.backend_pid, locktype="advisory")
                assert not task.done()  # blocked on the resource lock
            # leaving the lock + transaction releases the lock
            outcome = await task
            assert outcome.granted is True

    asyncio.run(_run())


def test_admit_two_calls_at_cap_one_grant_one_deny(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget_quota(conn)
            first = await _admit(conn, res)
            second = await _admit(conn, res)
            assert first.granted is True
            assert second.granted is False
            assert await _count_allocs(conn) == 1

    asyncio.run(_run())


def test_admit_denies_explicit_scoped_resource_for_foreign_project(migrated_url: str) -> None:
    # The selection filter excludes a foreign-scoped host, but an explicit resource_id can
    # still target one; admit is the backstop and hard-denies it (Task 4.2).
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=2, owner_project="other")
            await _seed_budget_quota(conn, project="mine")
            outcome = await _admit(conn, res, project="mine")
            assert outcome.granted is False
            assert outcome.allocation is None
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED
            assert await _count_allocs(conn) == 0  # no durable write
            assert await _count_audit(conn) == 0

    asyncio.run(_run())


def test_admit_grants_global_resource_to_any_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=2)  # owner_project NULL == global
            await _seed_budget_quota(conn, project="mine")
            outcome = await _admit(conn, res, project="mine")
            assert outcome.granted is True

    asyncio.run(_run())


def test_admit_grants_owned_resource_to_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=2, owner_project="mine")
            await _seed_budget_quota(conn, project="mine")
            outcome = await _admit(conn, res, project="mine")
            assert outcome.granted is True

    asyncio.run(_run())


def test_admit_grants_allowlisted_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(
                conn, cap=2, owner_project="other", affinity_allowlist=["mine"]
            )
            await _seed_budget_quota(conn, project="mine")
            outcome = await _admit(conn, res, project="mine")
            assert outcome.granted is True

    asyncio.run(_run())
