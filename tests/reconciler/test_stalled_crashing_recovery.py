"""The reconciler recovery for a stalled `crashing` System (ADR-0325, #1078).

`force_crash` commits `crashing` before firing the physical NMI. If the handler then dies
(worker crash) or is operator-canceled after the marker, the System would strand in `crashing`
forever with power blocked. `repair_stalled_crashing_systems` resolves a `crashing` System with
no active (`queued`/`running`) `force_crash` job to `crashed` (evidence-first): the NMI has
overwhelmingly fired, so the crash workflow (`capture_vmcore` -> teardown) can proceed. A System
whose `force_crash` job is still reclaimable (running with a valid or lapsed lease, or queued) is
left for the worker/retry path.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import DebugSessionState, JobState, SystemState
from kdive.reconciler.repairs.systems import repair_stalled_crashing_systems
from tests.reconciler.conftest import connect, run_repair, seed_debug_session, seed_run, seed_system


async def _seed_force_crash_job(
    conn: psycopg.AsyncConnection,
    system_id: UUID,
    *,
    state: str,
    lease_seconds: int = 300,
    attempt: int = 1,
    max_attempts: int = 3,
) -> None:
    """Insert a force_crash job for ``system_id`` with the stable dedup_key and given state."""
    payload: dict[str, Any] = {"system_id": str(system_id)}
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
        "    lease_expires_at, authorizing, dedup_key) "
        "VALUES ('force_crash', %s, %s, %s, %s, 'w', now() + make_interval(secs => %s), "
        "    %s, %s)",
        (
            Jsonb(payload),
            state,
            attempt,
            max_attempts,
            lease_seconds,
            Jsonb({"principal": "reconciler-test", "agent_session": None, "project": "test"}),
            f"{system_id}:force_crash",
        ),
    )


async def _system_state(conn: psycopg.AsyncConnection, system_id: UUID) -> str:
    cur = await conn.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _audit_count(conn: psycopg.AsyncConnection, *, object_kind: str, transition: str) -> int:
    row = await (
        await conn.execute(
            "SELECT count(*) FROM audit_log WHERE object_kind = %s AND transition = %s "
            "AND principal = 'system:reconciler'",
            (object_kind, transition),
        )
    ).fetchone()
    assert row is not None
    return row[0]


def test_recovers_crashing_with_failed_job(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.CRASHING)
        await _seed_force_crash_job(conn, system_id, state=JobState.FAILED.value)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_crashing_systems)
        assert recovered == 1
        assert await _system_state(conn, system_id) == SystemState.CRASHED.value
        assert await _audit_count(conn, object_kind="systems", transition="crashing->crashed") == 1
        await conn.close()

    asyncio.run(_run())


def test_recovers_crashing_with_canceled_job(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.CRASHING)
        await _seed_force_crash_job(conn, system_id, state=JobState.CANCELED.value)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_crashing_systems)
        assert recovered == 1
        assert await _system_state(conn, system_id) == SystemState.CRASHED.value
        await conn.close()

    asyncio.run(_run())


def test_recovers_crashing_with_no_job_row(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.CRASHING)
        # No force_crash job at all (invariant-only backstop).
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_crashing_systems)
        assert recovered == 1
        assert await _system_state(conn, system_id) == SystemState.CRASHED.value
        await conn.close()

    asyncio.run(_run())


def test_recovers_multiple_crashing_systems_all_counted(migrated_url: str) -> None:
    # Each stalled crashing System is recovered and increments the tally: the return is the
    # number recovered, not a fixed 1.
    async def _run() -> None:
        conn = await connect(migrated_url)
        ids = [
            await seed_system(conn, system_state=SystemState.CRASHING) for _ in range(3)
        ]  # no force_crash jobs → all stalled
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_crashing_systems)
        assert recovered == 3  # every recovered System counted, not a fixed 1
        for sid in ids:
            assert await _system_state(conn, sid) == SystemState.CRASHED.value
        await conn.close()

    asyncio.run(_run())


def test_recovers_crashing_detaches_live_session(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.CRASHING)
        run_id = await seed_run(conn, system_id)
        session_id = await seed_debug_session(conn, run_id, state=DebugSessionState.LIVE)
        await _seed_force_crash_job(conn, system_id, state=JobState.FAILED.value)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_crashing_systems)
        assert recovered == 1
        assert await _system_state(conn, system_id) == SystemState.CRASHED.value
        row = await (
            await conn.execute("SELECT state FROM debug_sessions WHERE id = %s", (session_id,))
        ).fetchone()
        assert row is not None and row[0] == DebugSessionState.DETACHED.value
        assert (
            await _audit_count(conn, object_kind="debug_sessions", transition="live->detached") == 1
        )
        await conn.close()

    asyncio.run(_run())


def test_leaves_crashing_with_running_valid_lease(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.CRASHING)
        await _seed_force_crash_job(
            conn, system_id, state=JobState.RUNNING.value, lease_seconds=300
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_crashing_systems)
        assert recovered == 0
        assert await _system_state(conn, system_id) == SystemState.CRASHING.value
        await conn.close()

    asyncio.run(_run())


def test_leaves_crashing_with_queued_job(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.CRASHING)
        await _seed_force_crash_job(conn, system_id, state=JobState.QUEUED.value)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_crashing_systems)
        assert recovered == 0
        assert await _system_state(conn, system_id) == SystemState.CRASHING.value
        await conn.close()

    asyncio.run(_run())


def test_leaves_crashing_with_lease_lapsed_running(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.CRASHING)
        # Lease lapsed but attempts remain: a worker will re-dequeue it in place; leave it alone.
        await _seed_force_crash_job(
            conn,
            system_id,
            state=JobState.RUNNING.value,
            lease_seconds=-60,
            attempt=1,
            max_attempts=3,
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_crashing_systems)
        assert recovered == 0
        assert await _system_state(conn, system_id) == SystemState.CRASHING.value
        await conn.close()

    asyncio.run(_run())
