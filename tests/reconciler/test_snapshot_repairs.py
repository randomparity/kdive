"""Reconciler recovery for stranded restore/snapshot (#1254, ADR-0378).

A `restoring` System with no active `restore` job strands forever (fenced from every lifecycle
op); a `creating` snapshot row with no active `snapshot` job wedges its name. Both repairs resolve
the stranded state to `failed`. A still-active job is left for the retry path.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SNAPSHOTS
from kdive.domain.capacity.state import JobState, SnapshotState, SystemState
from kdive.domain.lifecycle.records import Snapshot
from kdive.reconciler.repairs.systems import (
    repair_stalled_creating_snapshots,
    repair_stalled_restoring_systems,
)
from tests.reconciler.conftest import connect, run_repair, seed_system

_DT_PRINCIPAL = "user-1"


async def _seed_job(
    conn: psycopg.AsyncConnection, kind: str, payload: dict[str, Any], *, state: str
) -> None:
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
        "    lease_expires_at, authorizing, dedup_key) "
        "VALUES (%s, %s, %s, 1, 3, 'w', now() + make_interval(secs => 300), %s, %s)",
        (
            kind,
            Jsonb(payload),
            state,
            Jsonb({"principal": "t", "agent_session": None, "project": "proj"}),
            f"{uuid4()}",
        ),
    )


async def _seed_snapshot(
    conn: psycopg.AsyncConnection, system_id: UUID, name: str, state: SnapshotState
) -> UUID:
    row = await SNAPSHOTS.insert(
        conn,
        Snapshot(
            id=uuid4(),
            created_at=datetime(2026, 7, 17, tzinfo=UTC),
            updated_at=datetime(2026, 7, 17, tzinfo=UTC),
            principal=_DT_PRINCIPAL,
            project="proj",
            system_id=system_id,
            name=name,
            include_memory=True,
            state=state,
        ),
    )
    return row.id


async def _system_state(conn: psycopg.AsyncConnection, system_id: UUID) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _snapshot_state(conn: psycopg.AsyncConnection, snapshot_id: UUID) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT state FROM snapshots WHERE id = %s", (snapshot_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


def test_recovers_restoring_with_no_active_restore_job(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.RESTORING)
        await _seed_job(conn, "restore", {"system_id": str(sid)}, state=JobState.FAILED.value)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_restoring_systems)
        assert recovered == 1
        assert await _system_state(conn, sid) == SystemState.FAILED.value
        await conn.close()

    asyncio.run(_run())


def test_leaves_restoring_with_active_restore_job(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.RESTORING)
        await _seed_job(conn, "restore", {"system_id": str(sid)}, state=JobState.RUNNING.value)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_restoring_systems)
        assert recovered == 0
        assert await _system_state(conn, sid) == SystemState.RESTORING.value
        await conn.close()

    asyncio.run(_run())


def test_repair_no_ops_a_restore_that_committed_ready(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        # A succeeded restore already committed READY before its job went terminal.
        sid = await seed_system(conn, system_state=SystemState.READY)
        await _seed_job(conn, "restore", {"system_id": str(sid)}, state=JobState.SUCCEEDED.value)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_restoring_systems)
        assert recovered == 0
        assert await _system_state(conn, sid) == SystemState.READY.value
        await conn.close()

    asyncio.run(_run())


def test_recovers_creating_snapshot_with_no_active_job(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.READY)
        snap_id = await _seed_snapshot(conn, sid, "cp", SnapshotState.CREATING)
        await _seed_job(
            conn, "snapshot", {"snapshot_id": str(snap_id)}, state=JobState.FAILED.value
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_creating_snapshots)
        assert recovered == 1
        assert await _snapshot_state(conn, snap_id) == SnapshotState.FAILED.value
        await conn.close()

    asyncio.run(_run())


def test_leaves_creating_snapshot_with_active_job(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.READY)
        snap_id = await _seed_snapshot(conn, sid, "cp", SnapshotState.CREATING)
        await _seed_job(
            conn, "snapshot", {"snapshot_id": str(snap_id)}, state=JobState.RUNNING.value
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_creating_snapshots)
        assert recovered == 0
        assert await _snapshot_state(conn, snap_id) == SnapshotState.CREATING.value
        await conn.close()

    asyncio.run(_run())
