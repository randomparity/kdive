"""The reconciler orphaned-`active` allocation reaper (ADR-0109 #371, ADR-0480 #1628).

A failed/interrupted lifecycle run leaves an allocation `active` while its single System has
reached a terminal state (`torn_down`/`failed`) — the teardown job never releases the
allocation — so it permanently holds its host-cap slot and wedges a `cap=1` host. The reaper
releases such an allocation (`active -> releasing -> released`, with the `active_ended_at`
stamp and the single `reconciled` credit), preserves an allocation whose System is still live,
and waits out a grace window to avoid a mid-provision race. It is idempotent and per-candidate
isolated.

A `crashed` System is the hard case: it is the state an *in-progress* crash investigation sits
in **and** the state an aborted run strands its allocation in. It is therefore live only while
its investigation shows activity — a System-row write, an active or recently-touched job naming
it, or a non-terminal / recently-touched DebugSession on one of its Runs. Silence for
`crashed_idle_grace` makes it reclaimable. Both directions are pinned below.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES, SYSTEMS
from kdive.domain.accounting.cost import Selector
from kdive.domain.accounting.records import Budget, Quota
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    ResourceStatus,
    SystemState,
)
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, System
from kdive.mcp.auth import RequestContext
from kdive.providers.infra.reaping import NullReaper
from kdive.reconciler import loop
from kdive.reconciler.repairs import allocations as allocation_repairs
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation.admission.core import AllocationRequest, admit
from tests.db_waits import wait_until_any_backend_waiting
from tests.reconcile_helpers import make_reconcile_config
from tests.reconciler.conftest import connect, run_repair, seed_debug_session, seed_run

_DT = datetime(2026, 1, 1, tzinfo=UTC)

#: Older than ``DEFAULT_CRASHED_IDLE_GRACE`` (30 min), so a `crashed` System aged by this much
#: with no other activity reads as an abandoned investigation.
_IDLE = timedelta(hours=2)
#: Well inside the crashed-idle grace: an investigation that is still producing activity.
_FRESH = timedelta(seconds=1)


async def _age_updated_at(
    conn: psycopg.AsyncConnection, table: str, row_id: UUID, age: timedelta
) -> None:
    """Set ``<table>.updated_at = now() - age``, bypassing that table's set_updated_at trigger.

    Every one of these tables carries a ``<table>_set_updated_at`` trigger that rewrites
    ``updated_at := now()`` on any row-changing UPDATE, so a plain UPDATE cannot age it. The
    trigger is disabled for the single aging statement, simulating a row whose last write was
    ``age`` ago. Identifiers go through ``sql.Identifier`` rather than an f-string.
    """
    trigger = sql.Identifier(f"{table}_set_updated_at")
    relation = sql.Identifier(table)
    await conn.execute(
        sql.SQL("ALTER TABLE {relation} DISABLE TRIGGER {trigger}").format(
            relation=relation, trigger=trigger
        )
    )
    try:
        await conn.execute(
            sql.SQL("UPDATE {relation} SET updated_at = now() - %s WHERE id = %s").format(
                relation=relation
            ),
            (age, row_id),
        )
    finally:
        await conn.execute(
            sql.SQL("ALTER TABLE {relation} ENABLE TRIGGER {trigger}").format(
                relation=relation, trigger=trigger
            )
        )


async def _system_id_for(conn: psycopg.AsyncConnection, alloc_id: UUID) -> UUID:
    cur = await conn.execute("SELECT id FROM systems WHERE allocation_id = %s", (alloc_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _seed_job(
    conn: psycopg.AsyncConnection,
    system_id: UUID,
    *,
    state: str,
    age: timedelta,
    kind: str = "capture_vmcore",
    principal: str = "alice",
) -> UUID:
    """Insert a job naming ``system_id`` in ``payload``, with ``updated_at = now() - age``."""
    cur = await conn.execute(
        "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
        "VALUES (%s, %s, %s, 3, %s, %s) RETURNING id",
        (
            kind,
            Jsonb({"system_id": str(system_id)}),
            state,
            Jsonb({"principal": principal, "agent_session": None, "project": "proj"}),
            f"{system_id}:{kind}:{uuid4()}",
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    job_id: UUID = row[0]
    await _age_updated_at(conn, "jobs", job_id, age)
    return job_id


async def _seed_session_on(
    conn: psycopg.AsyncConnection,
    system_id: UUID,
    *,
    state: DebugSessionState,
    age: timedelta,
) -> UUID:
    """Insert a Run on ``system_id`` plus a DebugSession, aged to ``now() - age``."""
    run_id = await seed_run(conn, system_id)
    session_id = await seed_debug_session(conn, run_id, state=state)
    await _age_updated_at(conn, "debug_sessions", session_id, age)
    return session_id


async def _seed_active_alloc(
    conn: psycopg.AsyncConnection,
    *,
    system_state: SystemState | None = SystemState.TORN_DOWN,
    updated_age: timedelta = timedelta(minutes=10),
    system_age: timedelta = _IDLE,
    with_budget: bool = True,
    sized: bool = True,
) -> UUID:
    """Seed resource (+budget) -> active allocation (-> System in ``system_state``).

    ``system_state=None`` seeds no System row. ``updated_age`` ages the allocation's
    ``updated_at`` to ``now() - updated_age`` and ``system_age`` does the same for the System
    row (both set in SQL, DB clock). The allocation is sized and reserved so a release writes a
    real ``reconciled`` credit.
    """
    resource = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={},
            pool="p",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    if with_budget:
        await BUDGETS.upsert(
            conn,
            Budget(project="proj", limit_kcu=Decimal("1000"), spent_kcu=Decimal(0), updated_at=_DT),
        )
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource.id,
            state=AllocationState.ACTIVE,
            requested_vcpus=2 if sized else None,
            requested_memory_gb=4 if sized else None,
            active_started_at=datetime.now(UTC) - timedelta(hours=1),
        ),
    )
    if with_budget:
        await accounting.reserve(conn, alloc, Decimal("9.0000"))
    if system_state is not None:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                allocation_id=alloc.id,
                state=system_state,
                provisioning_profile={"k": "v"},
            ),
        )
        await _age_updated_at(conn, "systems", system.id, system_age)
    # Age updated_at in SQL so there is no test-vs-Postgres clock skew. Done last because each
    # insert/update bumps updated_at (via the trigger this helper bypasses).
    await _age_updated_at(conn, "allocations", alloc.id, updated_age)
    return alloc.id


async def _alloc_state(conn: psycopg.AsyncConnection, alloc_id: UUID) -> str:
    cur = await conn.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _ledger_kinds(conn: psycopg.AsyncConnection, alloc_id: UUID) -> list[str]:
    cur = await conn.execute(
        "SELECT event_type FROM ledger WHERE allocation_id = %s ORDER BY ts, id", (alloc_id,)
    )
    return [r[0] for r in await cur.fetchall()]


def test_leaked_active_with_torn_down_system_reclaimed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"
            assert await _ledger_kinds(check, alloc_id) == ["reserved", "reconciled"]
            cur = await check.execute(
                "SELECT active_ended_at FROM allocations WHERE id = %s", (alloc_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] is not None  # active_ended_at stamped

    asyncio.run(_run())


def test_multiple_orphaned_active_allocations_all_counted(migrated_url: str) -> None:
    # Each orphaned active allocation increments the reclaimed tally: the return is the number
    # reclaimed, not a fixed 1.
    async def _run() -> None:
        ids: list[UUID] = []
        async with await connect(migrated_url) as seed:
            for _ in range(3):
                ids.append(await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 3  # every reclaimed allocation counted, not a fixed 1
        async with await connect(migrated_url) as check:
            for alloc_id in ids:
                assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_leaked_active_with_failed_system_reclaimed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.FAILED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_leaked_active_with_no_system_reclaimed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=None)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_active_with_ready_system_preserved(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.READY)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"  # untouched

    asyncio.run(_run())


def test_active_with_provisioning_system_preserved(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.PROVISIONING)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"

    asyncio.run(_run())


def test_active_with_crashing_system_preserved(migrated_url: str) -> None:
    # `crashing` is mid-force_crash and unconditionally live: no idle window applies, however
    # long the row has sat. `repair_stalled_crashing_systems` is what resolves a stuck one.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHING, system_age=timedelta(days=7)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"

    asyncio.run(_run())


def test_active_with_recently_crashed_system_preserved(migrated_url: str) -> None:
    # THE regression this whole design exists to prevent: an allocation backing an in-progress
    # crash investigation must NOT be reaped. Just-crashed, inside the idle window -> live.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=_FRESH
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"

    asyncio.run(_run())


def test_idle_crashed_system_reclaimed(migrated_url: str) -> None:
    # #1628: a run that aborted between crashing its System and releasing the allocation. The
    # System sits `crashed` with nothing else happening, so the slot is stranded until the 4h
    # lease. Past the idle window with no job and no session, it is reclaimed.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=_IDLE
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"
            assert await _ledger_kinds(check, alloc_id) == ["reserved", "reconciled"]

    asyncio.run(_run())


def test_idle_crashed_system_with_queued_capture_job_preserved(migrated_url: str) -> None:
    # A capture the agent asked for but the worker has not started yet: the job is `queued`, so
    # the investigation is live no matter how long the System row has sat untouched.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=_IDLE
            )
            await _seed_job(seed, await _system_id_for(seed, alloc_id), state="queued", age=_IDLE)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"

    asyncio.run(_run())


def test_idle_crashed_system_with_recently_finished_job_preserved(migrated_url: str) -> None:
    # The four-method capstone shape: a capture just succeeded and the agent is choosing the
    # next method. No active job, no session, a System row stamped at the crash — only the
    # finished job's recency says someone is still here, and it must be enough.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=_IDLE
            )
            await _seed_job(
                seed, await _system_id_for(seed, alloc_id), state="succeeded", age=_FRESH
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"

    asyncio.run(_run())


def test_idle_crashed_system_with_stale_finished_job_reclaimed(migrated_url: str) -> None:
    # The complement of the test above: a job that finished long ago is history, not activity.
    # Without this the job clause would read as "ever had a job" and never expire.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=_IDLE
            )
            await _seed_job(
                seed, await _system_id_for(seed, alloc_id), state="succeeded", age=_IDLE
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_idle_crashed_system_with_reconciler_authored_job_reclaimed(migrated_url: str) -> None:
    # The vacuity guard. `sweep_console_rotation` enqueues a fresh `console_rotate` job for every
    # live local System — `crashed` included — on EVERY pass, forever. If reconciler-authored
    # jobs counted as investigation activity, the job signal would be permanently true and this
    # repair would never fire on the local provider, however long the slot had been stranded.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=_IDLE
            )
            await _seed_job(
                seed,
                await _system_id_for(seed, alloc_id),
                state="queued",
                age=_FRESH,
                kind="console_rotate",
                principal=allocation_repairs.SYSTEM_RECONCILER_PRINCIPAL,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_idle_crashed_system_with_live_debug_session_preserved(migrated_url: str) -> None:
    # A drgn/gdb session attached to the crashed guest is the analysis half of the workflow. A
    # non-terminal session keeps the allocation live even when the row itself is stale — an
    # agent can sit on an open session reading memory without writing a single row.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=_IDLE
            )
            await _seed_session_on(
                seed,
                await _system_id_for(seed, alloc_id),
                state=DebugSessionState.LIVE,
                age=_IDLE,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"

    asyncio.run(_run())


def test_idle_crashed_system_with_recently_detached_session_preserved(migrated_url: str) -> None:
    # A session the agent just detached from is still activity; it will likely attach again.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=_IDLE
            )
            await _seed_session_on(
                seed,
                await _system_id_for(seed, alloc_id),
                state=DebugSessionState.DETACHED,
                age=_FRESH,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"

    asyncio.run(_run())


def test_idle_crashed_system_with_stale_detached_session_reclaimed(migrated_url: str) -> None:
    # A long-detached session is the exact shape the reconciler's own dead-session sweep leaves
    # behind when a worker dies, so it must not pin the slot forever.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=_IDLE
            )
            await _seed_session_on(
                seed,
                await _system_id_for(seed, alloc_id),
                state=DebugSessionState.DETACHED,
                age=_IDLE,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_crashed_idle_grace_widens_the_live_window(migrated_url: str) -> None:
    # The grace is the operator's brake: the same idle crashed System is preserved under a wide
    # window and reclaimed under a narrow one. Proves the parameter is load-bearing, not inert.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=timedelta(minutes=45)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            wide = await run_repair(
                pool,
                lambda conn: allocation_repairs.reap_orphaned_active_allocations(
                    conn, crashed_idle_grace=timedelta(hours=4)
                ),
            )
            assert wide == 0
            narrow = await run_repair(
                pool,
                lambda conn: allocation_repairs.reap_orphaned_active_allocations(
                    conn, crashed_idle_grace=timedelta(minutes=5)
                ),
            )
        assert narrow == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_within_grace_preserved_then_reclaimed_after_aging(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            # Freshly settled (updated_at ~ now): inside the 2-min grace window.
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.TORN_DOWN, updated_age=timedelta(seconds=1)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
            assert first == 0  # within grace
            async with await connect(migrated_url) as age:
                await _age_updated_at(age, "allocations", alloc_id, timedelta(minutes=10))
            second = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert second == 1  # past grace now
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_reap_second_pass_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
            second = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert first == 1
        assert second == 0  # already released
        async with await connect(migrated_url) as check:
            reconciled = [k for k in await _ledger_kinds(check, alloc_id) if k == "reconciled"]
            assert len(reconciled) == 1  # idempotent: one credit despite two passes

    asyncio.run(_run())


def test_unpriceable_leaked_active_does_not_starve_siblings(migrated_url: str) -> None:
    # An unsized active allocation cannot be reconciled; its per-candidate transaction rolls
    # back and it stays `active` for retry, while a valid sibling is still reclaimed.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            bad = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN, sized=False)
            good = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1  # only the good one
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, good) == "released"
            assert await _alloc_state(check, bad) == "active"  # rolled back, retried next pass

    asyncio.run(_run())


def test_concurrent_release_vs_reap_reconciles_once(migrated_url: str) -> None:
    # The reaper and a release both take PROJECT -> ALLOCATION. A holder pre-takes the locks
    # and releases the allocation while the reaper blocks; the reaper then sees a terminal
    # state under the lock and skips, so exactly one reconciled row exists.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN)
        holder = await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)
        try:
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                async with (
                    holder.transaction(),
                    advisory_xact_lock(holder, LockScope.PROJECT, "proj"),
                    advisory_xact_lock(holder, LockScope.ALLOCATION, alloc_id),
                ):
                    task = asyncio.ensure_future(
                        run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
                    )
                    await wait_until_any_backend_waiting(holder, locktype="advisory")
                    assert not task.done()  # blocked behind the held locks
                    await ALLOCATIONS.update_state(holder, alloc_id, AllocationState.RELEASING)
                    alloc = await ALLOCATIONS.update_state(
                        holder, alloc_id, AllocationState.RELEASED
                    )
                    await accounting.reconcile(holder, alloc)
                count = await task
        finally:
            await holder.close()
        assert count == 0  # the reaper lost the race and skipped the now-terminal allocation
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"
            reconciled = [k for k in await _ledger_kinds(check, alloc_id) if k == "reconciled"]
            assert len(reconciled) == 1

    asyncio.run(_run())


async def _seed_capped_resource(conn: psycopg.AsyncConnection) -> Resource:
    """A cap=1 resource with the capabilities + project quota the promotion sweep needs."""
    resource = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={CONCURRENT_ALLOCATION_CAP_KEY: 1, "vcpus": 64, "memory_mb": 65536},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    await BUDGETS.upsert(
        conn,
        Budget(project="proj", limit_kcu=Decimal("1000000"), spent_kcu=Decimal(0), updated_at=_DT),
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
    return resource


async def _seed_leaked_active_on(conn: psycopg.AsyncConnection, resource: Resource) -> UUID:
    """An active allocation on ``resource`` whose System is torn_down, aged past grace."""
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource.id,
            state=AllocationState.ACTIVE,
            requested_vcpus=1,
            requested_memory_gb=0,
            active_started_at=datetime.now(UTC) - timedelta(hours=1),
        ),
    )
    await accounting.reserve(conn, alloc, Decimal("9.0000"))
    await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            allocation_id=alloc.id,
            state=SystemState.TORN_DOWN,
            provisioning_profile={"k": "v"},
        ),
    )
    await _age_updated_at(conn, "allocations", alloc.id, timedelta(minutes=10))
    return alloc.id


async def _enqueue_request(conn: psycopg.AsyncConnection, resource: Resource) -> UUID:
    """Queue a `requested` row on the at-capacity ``resource`` via admit(on_capacity=queue)."""
    ctx = RequestContext(principal="bob", agent_session="sess", projects=("proj",))
    outcome = await admit(
        conn,
        AllocationRequest(
            ctx=ctx,
            resource=resource,
            project="proj",
            selector=Selector(vcpus=1, memory_gb=0, cost_class="local"),
            window=1,
            on_capacity="queue",
            disk_gb=10,
            requested_kind=ResourceKind.LOCAL_LIBVIRT,
        ),
    )
    assert outcome.granted and outcome.allocation is not None
    assert outcome.allocation.state is AllocationState.REQUESTED
    return outcome.allocation.id


def test_reconcile_once_reports_counter_and_frees_slot_same_pass(migrated_url: str) -> None:
    # One reconcile_once pass reaps the leaked active allocation and the promotion sweep
    # (run right after) fills the freed cap=1 slot with a queued request on the same resource.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            resource = await _seed_capped_resource(seed)
            leaked = await _seed_leaked_active_on(seed, resource)
            queued = await _enqueue_request(seed, resource)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await loop.reconcile_once(pool, NullReaper(), config=make_reconcile_config())
        assert report.reaped_active_allocations == 1
        assert report.promoted_allocations == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, leaked) == "released"
            assert await _alloc_state(check, queued) == "granted"  # filled the freed slot

    asyncio.run(_run())


def test_reconcile_once_threads_the_configured_crashed_idle_grace(migrated_url: str) -> None:
    # The knob is only worth having if the pass hands it down. A System idle for 45 minutes is
    # past the 30-minute default, so the default pass reaps it; a configured 4-hour grace must
    # preserve the very same row, which can only happen if ReconcileConfig.crashed_idle_grace
    # reaches the repair. A knob that never left the dataclass would fail the first assertion.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.CRASHED, system_age=timedelta(minutes=45)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            wide = await loop.reconcile_once(
                pool,
                NullReaper(),
                config=make_reconcile_config(crashed_idle_grace=timedelta(hours=4)),
            )
            assert wide.reaped_active_allocations == 0
            async with await connect(migrated_url) as check:
                assert await _alloc_state(check, alloc_id) == "active"
            default = await loop.reconcile_once(pool, NullReaper(), config=make_reconcile_config())
        assert default.reaped_active_allocations == 1  # past the 30-min default
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())
