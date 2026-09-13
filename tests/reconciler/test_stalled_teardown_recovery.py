"""The reconciler requeues an ordinary teardown stranded at its durable fence (#2370)."""

from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import JobState, SystemState
from kdive.reconciler import loop
from kdive.reconciler.repairs import systems as system_repairs
from kdive.reconciler.repairs.systems import repair_stalled_tearing_down_systems
from tests.reconciler.conftest import connect, run_repair, seed_system


async def _seed_teardown_job(
    conn: psycopg.AsyncConnection,
    system_id: UUID,
    *,
    state: JobState,
    attempt: int = 3,
) -> None:
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key) "
        "VALUES ('teardown', %s, %s, %s, 3, %s, %s)",
        (
            Jsonb({"system_id": str(system_id)}),
            state.value,
            attempt,
            Jsonb({"principal": "reconciler-test", "agent_session": None, "project": "proj"}),
            f"{system_id}:teardown",
        ),
    )


async def _teardown_job(conn: psycopg.AsyncConnection, system_id: UUID) -> tuple[str, int] | None:
    row = await (
        await conn.execute(
            "SELECT state, attempt FROM jobs WHERE dedup_key = %s", (f"{system_id}:teardown",)
        )
    ).fetchone()
    return None if row is None else (str(row[0]), int(row[1]))


@pytest.mark.parametrize("state", [JobState.FAILED, JobState.SUCCEEDED])
def test_requeues_terminal_teardown_for_live_allocation(migrated_url: str, state: JobState) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        await _seed_teardown_job(conn, system_id, state=state)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            assert await run_repair(pool, repair_stalled_tearing_down_systems) == 1
        assert await _teardown_job(conn, system_id) == (JobState.QUEUED.value, 0)
        row = await (
            await conn.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        ).fetchone()
        assert row is not None and row[0] == SystemState.TEARING_DOWN.value
        await conn.close()

    asyncio.run(_run())


def test_repair_runs_after_abandoned_jobs() -> None:
    kinds = loop.ALL_REPAIR_KINDS

    assert "stalled_tearing_down_systems" in kinds
    assert kinds.index("abandoned_jobs") < kinds.index("stalled_tearing_down_systems")


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.RUNNING, JobState.CANCELED])
def test_leaves_active_or_canceled_teardown_untouched(migrated_url: str, state: JobState) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        await _seed_teardown_job(conn, system_id, state=state, attempt=1)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            assert await run_repair(pool, repair_stalled_tearing_down_systems) == 0
        assert await _teardown_job(conn, system_id) == (state.value, 1)
        await conn.close()

    asyncio.run(_run())


def test_creates_missing_teardown_job(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            assert await run_repair(pool, repair_stalled_tearing_down_systems) == 1
        assert await _teardown_job(conn, system_id) == (JobState.QUEUED.value, 0)
        await conn.close()

    asyncio.run(_run())


def test_respects_stalled_teardown_candidate_limit(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        first = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        second = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        monkeypatch.setattr(system_repairs, "_STALLED_TEARING_DOWN_REPAIR_LIMIT", 1)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            assert await run_repair(pool, repair_stalled_tearing_down_systems) == 1
        states = [await _teardown_job(conn, system_id) for system_id in (first, second)]
        assert states.count((JobState.QUEUED.value, 0)) == 1
        assert states.count(None) == 1
        await conn.close()

    asyncio.run(_run())


def test_canceled_job_does_not_starve_bounded_replay(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_ids = sorted(
            [
                await seed_system(conn, system_state=SystemState.TEARING_DOWN),
                await seed_system(conn, system_state=SystemState.TEARING_DOWN),
            ]
        )
        canceled, failed = system_ids
        await _seed_teardown_job(conn, canceled, state=JobState.CANCELED)
        await _seed_teardown_job(conn, failed, state=JobState.FAILED)
        monkeypatch.setattr(system_repairs, "_STALLED_TEARING_DOWN_REPAIR_LIMIT", 1)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            assert await run_repair(pool, repair_stalled_tearing_down_systems) == 1
        assert await _teardown_job(conn, canceled) == (JobState.CANCELED.value, 3)
        assert await _teardown_job(conn, failed) == (JobState.QUEUED.value, 0)
        await conn.close()

    asyncio.run(_run())
