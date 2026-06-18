"""Concurrency races for ``runs.bind`` (ADR-0169).

Two binds that touch the same Run or the same System must converge to exactly one winner: the
``IS NULL`` compare-and-set guards a double-bind of one Run, and the per-System lock plus the
one-Run-per-System precondition guard two Runs racing for one System.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import LiteralString
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle import Allocation, Investigation, Run, System
from kdive.mcp.tools.lifecycle.runs.bind import RunBindRequest, bind_run
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from tests.db.conftest import migrated_url  # noqa: F401

_DT = datetime(2026, 6, 18, tzinfo=None).replace(tzinfo=None)


def _ctx() -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
    )


async def _seed_ready_system(pool: AsyncConnectionPool) -> str:
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
                state=AllocationState.ACTIVE,
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
                state=SystemState.READY,
                provisioning_profile={},
            ),
        )
    return str(system.id)


async def _seed_unbound_run(
    pool: AsyncConnectionPool, *, state: RunState = RunState.SUCCEEDED
) -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
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
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                investigation_id=inv.id,
                system_id=None,
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=state,
                build_profile={},
            ),
        )
    return str(run.id)


async def _bind(pool: AsyncConnectionPool, run_id: str, sys_id: str):
    return await bind_run(pool, _ctx(), RunBindRequest(run_id=run_id, system_id=sys_id))


async def _count(pool: AsyncConnectionPool, query: LiteralString, params: tuple) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_concurrent_bind_of_one_run_one_wins(migrated_url: str) -> None:  # noqa: F811
    """Two binds of the SAME unbound Run: the IS NULL CAS lets exactly one win."""

    async def _run() -> None:
        pool = AsyncConnectionPool(migrated_url, min_size=2, max_size=4, open=False)
        await pool.open()
        try:
            run_id = await _seed_unbound_run(pool)
            sys_a = await _seed_ready_system(pool)
            sys_b = await _seed_ready_system(pool)
            r1, r2 = await asyncio.gather(_bind(pool, run_id, sys_a), _bind(pool, run_id, sys_b))
            statuses = sorted([r1.status, r2.status])
            categories = {r.error_category for r in (r1, r2) if r.status == "error"}
            bound_count = await _count(
                pool, "SELECT count(*) FROM runs WHERE id = %s AND system_id IS NOT NULL", (run_id,)
            )
        finally:
            await pool.close()
        assert statuses == ["bound", "error"]
        assert categories == {"transport_conflict"}
        assert bound_count == 1

    asyncio.run(_run())


def test_concurrent_bind_to_one_system_one_wins(migrated_url: str) -> None:  # noqa: F811
    """Two different unbound Runs binding the SAME System: one-Run-per-System lets one win."""

    async def _run() -> None:
        pool = AsyncConnectionPool(migrated_url, min_size=2, max_size=4, open=False)
        await pool.open()
        try:
            sys_id = await _seed_ready_system(pool)
            # RUNNING (non-terminal) runs: a bound one counts toward one-Run-per-System, so the
            # second bind to the same System is rejected. A SUCCEEDED run would not block (it is
            # terminal for the one-Run-per-System count, matching runs.create semantics).
            run_a = await _seed_unbound_run(pool, state=RunState.RUNNING)
            run_b = await _seed_unbound_run(pool, state=RunState.RUNNING)
            r1, r2 = await asyncio.gather(_bind(pool, run_a, sys_id), _bind(pool, run_b, sys_id))
            statuses = sorted([r1.status, r2.status])
            categories = {r.error_category for r in (r1, r2) if r.status == "error"}
            live_on_system = await _count(
                pool, "SELECT count(*) FROM runs WHERE system_id = %s", (sys_id,)
            )
        finally:
            await pool.close()
        assert statuses == ["bound", "error"]
        assert categories == {"transport_conflict"}
        assert live_on_system == 1

    asyncio.run(_run())
