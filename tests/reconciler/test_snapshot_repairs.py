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
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Snapshot
from kdive.reconciler.repairs.systems import (
    repair_stalled_creating_snapshots,
    repair_stalled_restoring_systems,
)
from tests.reconciler.conftest import connect, run_repair, seed_system

_DT_PRINCIPAL = "user-1"


async def _seed_job(
    conn: psycopg.AsyncConnection,
    kind: str,
    payload: dict[str, Any],
    *,
    state: str,
    error_category: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
        "    lease_expires_at, authorizing, dedup_key, error_category) "
        "VALUES (%s, %s, %s, 1, 3, 'w', now() + make_interval(secs => 300), %s, %s, %s)",
        (
            kind,
            Jsonb(payload),
            state,
            Jsonb({"principal": "t", "agent_session": None, "project": "proj"}),
            f"{uuid4()}",
            error_category,
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


async def _system_failure_category(conn: psycopg.AsyncConnection, system_id: UUID) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT failure_category FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    assert row is not None
    return None if row[0] is None else str(row[0])


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
        # ADR-0513: the repair records *why*, so the System does not fall back to the
        # `infrastructure_failure` default the envelope uses for a category-less failed row.
        assert await _system_failure_category(conn, sid) == ErrorCategory.RESTORE_INCOMPLETE.value
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
        # The category is written only by the transition that earns it: a System left for the
        # retry path must not be labelled as though its restore had already been abandoned.
        assert await _system_failure_category(conn, sid) is None
        await conn.close()

    asyncio.run(_run())


def test_does_not_overwrite_a_restore_jobs_own_category(migrated_url: str) -> None:
    # ADR-0513: this repair's evidence is an *absence*, so it can hold weaker information than the
    # job row — `restore_handler` binds its snapshotter before the `try` that routes
    # restoring -> failed, so a CategorizedError there dead-letters the job with a real category
    # and never touches System state. A recorded category outranks the job's
    # (`_resolve_failure_verdict`), so stamping here would displace the real reason with a generic
    # one. Leave the column NULL and let the ADR-0454 job fallback answer.
    async def _run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.RESTORING)
        await _seed_job(
            conn,
            "restore",
            {"system_id": str(sid)},
            state=JobState.FAILED.value,
            error_category=ErrorCategory.CONFIGURATION_ERROR.value,
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_restoring_systems)
        # Still recovered — the System must leave `restoring` either way; only the stamp differs.
        assert recovered == 1
        assert await _system_state(conn, sid) == SystemState.FAILED.value
        assert await _system_failure_category(conn, sid) is None
        await conn.close()

    asyncio.run(_run())


def test_stamps_over_a_lease_expired_restore_job(migrated_url: str) -> None:
    # `repair_abandoned_jobs` stamps `lease_expired` unconditionally over a job that never
    # recorded a reason of its own — a statement about the lease, not about the guest. That is
    # not information worth deferring to, so the limbo verdict still wins.
    async def _run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.RESTORING)
        await _seed_job(
            conn,
            "restore",
            {"system_id": str(sid)},
            state=JobState.FAILED.value,
            error_category=ErrorCategory.LEASE_EXPIRED.value,
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_restoring_systems)
        assert recovered == 1
        assert await _system_failure_category(conn, sid) == ErrorCategory.RESTORE_INCOMPLETE.value
        await conn.close()

    asyncio.run(_run())


def test_stamps_when_no_restore_job_row_survives(migrated_url: str) -> None:
    # The job-less shape: a canceled restore whose row was never written, or the invariant-only
    # absent row. Nothing to defer to, so the verdict is recorded.
    async def _run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.RESTORING)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_restoring_systems)
        assert recovered == 1
        assert await _system_failure_category(conn, sid) == ErrorCategory.RESTORE_INCOMPLETE.value
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


def test_recovers_multiple_restoring_systems_all_counted(migrated_url: str) -> None:
    # Each stalled restoring System is recovered to failed and increments the tally.
    async def _run() -> None:
        conn = await connect(migrated_url)
        ids: list[UUID] = []
        for _ in range(3):
            sid = await seed_system(conn, system_state=SystemState.RESTORING)
            await _seed_job(conn, "restore", {"system_id": str(sid)}, state=JobState.FAILED.value)
            ids.append(sid)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_restoring_systems)
        assert recovered == 3  # every recovered System counted, not a fixed 1
        for sid in ids:
            assert await _system_state(conn, sid) == SystemState.FAILED.value
            assert (
                await _system_failure_category(conn, sid) == ErrorCategory.RESTORE_INCOMPLETE.value
            )
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


def test_recovers_multiple_creating_snapshots_all_counted(migrated_url: str) -> None:
    # Each stalled creating snapshot is recovered to failed and increments the tally.
    async def _run() -> None:
        conn = await connect(migrated_url)
        snap_ids: list[UUID] = []
        for i in range(3):
            sid = await seed_system(conn, system_state=SystemState.READY)
            snap_id = await _seed_snapshot(conn, sid, f"cp{i}", SnapshotState.CREATING)
            await _seed_job(
                conn, "snapshot", {"snapshot_id": str(snap_id)}, state=JobState.FAILED.value
            )
            snap_ids.append(snap_id)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            recovered = await run_repair(pool, repair_stalled_creating_snapshots)
        assert recovered == 3  # every recovered snapshot counted, not a fixed 1
        for snap_id in snap_ids:
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
