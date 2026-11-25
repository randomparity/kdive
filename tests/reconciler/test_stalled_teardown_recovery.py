"""The reconciler requeues an ordinary teardown stranded at its durable fence (#2370)."""

from __future__ import annotations

import asyncio
import logging

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import JobState, SystemState
from kdive.reconciler import loop
from kdive.reconciler.repairs import systems as system_repairs
from kdive.reconciler.repairs.systems import repair_stalled_tearing_down_systems
from tests.reconciler.conftest import connect, run_repair, seed_system
from tests.reconciler.recovery_support import (
    seed_completed_activation as _seed_completed_activation,
)
from tests.reconciler.recovery_support import seed_teardown_job as _seed_teardown_job
from tests.reconciler.recovery_support import teardown_job as _teardown_job


@pytest.mark.parametrize("state", [JobState.FAILED, JobState.SUCCEEDED])
def test_only_failed_retryable_teardown_is_requeued_for_live_allocation(
    migrated_url: str, state: JobState
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        await _seed_teardown_job(conn, system_id, state=state)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            assert await run_repair(pool, repair_stalled_tearing_down_systems) == (
                1 if state is JobState.FAILED else 0
            )
        expected = (JobState.QUEUED.value, 0) if state is JobState.FAILED else (state.value, 3)
        assert await _teardown_job(conn, system_id) == expected
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


def test_external_boot_history_is_not_requeued_and_warns_once(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)
    history = system_repairs.TeardownWarningHistory()

    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        await _seed_completed_activation(conn, system_id)
        await _seed_teardown_job(conn, system_id, state=JobState.FAILED)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            for _ in range(2):
                assert (
                    await run_repair(
                        pool,
                        lambda conn: repair_stalled_tearing_down_systems(conn, warnings=history),
                    )
                    == 0
                )
        assert await _teardown_job(conn, system_id) == (JobState.FAILED.value, 3)
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and str(system_id) in r.message
        ]
        assert len(warnings) == 1
        await conn.close()

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("key", "remedy"),
    [
        ("authority_system_v1", "no supported exit"),
        ("external_boot_authority_v1", "re-run systems.teardown"),
    ],
)
def test_authority_marked_row_is_not_overwritten_and_warns_once(
    migrated_url: str, key: str, remedy: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)
    history = system_repairs.TeardownWarningHistory()

    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        await _seed_teardown_job(conn, system_id, state=JobState.FAILED)
        await conn.execute(
            "UPDATE jobs SET payload = payload || %s WHERE dedup_key = %s",
            (Jsonb({key: {"marker": 1}}), f"{system_id}:teardown"),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            for _ in range(2):
                assert (
                    await run_repair(
                        pool,
                        lambda conn: repair_stalled_tearing_down_systems(conn, warnings=history),
                    )
                    == 0
                )
        assert await _teardown_job(conn, system_id) == (JobState.FAILED.value, 3)
        warnings = [r.message for r in caplog.records if str(system_id) in r.message]
        assert len(warnings) == 1
        assert remedy in warnings[0]
        await conn.close()

    asyncio.run(_run())


def test_warned_map_drops_a_system_that_leaves_tearing_down(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)
    history = system_repairs.TeardownWarningHistory()

    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        await _seed_completed_activation(conn, system_id)
        await _seed_teardown_job(conn, system_id, state=JobState.FAILED)
        set_state = "UPDATE systems SET state = %s WHERE id = %s"
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            await run_repair(
                pool, lambda conn: repair_stalled_tearing_down_systems(conn, warnings=history)
            )
            assert system_id in history.stalled
            await conn.execute(set_state, (SystemState.TORN_DOWN.value, system_id))
            await run_repair(
                pool, lambda conn: repair_stalled_tearing_down_systems(conn, warnings=history)
            )
            assert system_id not in history.stalled
            await conn.execute(set_state, (SystemState.TEARING_DOWN.value, system_id))
            await run_repair(
                pool, lambda conn: repair_stalled_tearing_down_systems(conn, warnings=history)
            )
        warnings = [r for r in caplog.records if str(system_id) in r.message]
        assert len(warnings) == 2
        await conn.close()

    asyncio.run(_run())
