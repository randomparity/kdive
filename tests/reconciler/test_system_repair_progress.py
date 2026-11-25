"""Each bounded System repair lane must advance past persistently failing candidates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from typing import Literal, cast
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.capacity.state import SystemState
from kdive.reconciler import loop
from kdive.reconciler.repairs import systems as repairs
from tests.reconcile_helpers import make_reconcile_config
from tests.reconciler.conftest import FakeReaper, connect, seed_run, seed_system

type Lane = Literal["leaked_mutation_obligations", "stalled_reprovisioning_systems"]


@pytest.fixture(params=["leaked_mutation_obligations", "stalled_reprovisioning_systems"])
def lane(request: pytest.FixtureRequest) -> Lane:
    return cast(Lane, request.param)


async def _seed(conn: psycopg.AsyncConnection, lane: Lane, indices: Iterable[int]) -> None:
    async with conn.transaction():
        for index in indices:
            system_id = UUID(int=index)
            state = (
                SystemState.TORN_DOWN
                if lane == "leaked_mutation_obligations"
                else SystemState.REPROVISIONING
            )
            previous = await seed_system(conn, system_state=state)
            await conn.execute("UPDATE systems SET id=%s WHERE id=%s", (system_id, previous))
            if lane == "leaked_mutation_obligations":
                run_id = await seed_run(conn, system_id)
                assert await RemoteModuleAttemptObligationRepository().open_mutation_obligation(
                    conn,
                    ModuleAttempt(system_id=system_id, run_id=run_id, operation_nonce="0" * 32),
                )


@asynccontextmanager
async def _hold_locks(
    conn: psycopg.AsyncConnection, indices: Iterable[int]
) -> AsyncGenerator[None]:
    async with conn.transaction():
        for index in indices:
            async with advisory_xact_lock(conn, LockScope.SYSTEM, UUID(int=index)):
                pass
        yield


async def _assert_repaired(conn: psycopg.AsyncConnection, lane: Lane, index: int) -> None:
    query = (
        "SELECT mutation_discharged_at IS NOT NULL FROM remote_module_attempt_obligations "
        "WHERE system_id=%s"
        if lane == "leaked_mutation_obligations"
        else "SELECT state='failed' FROM systems WHERE id=%s"
    )
    row = await (await conn.execute(query, (UUID(int=index),))).fetchone()
    assert row == (True,)


def test_a_full_failed_page_yields_to_a_healthy_successor(migrated_url: str, lane: Lane) -> None:
    async def run() -> None:
        async with await connect(migrated_url) as conn, await connect(migrated_url) as blocker:
            await _seed(conn, lane, range(1, 102))
            async with (
                _hold_locks(blocker, range(1, 101)),
                AsyncConnectionPool(
                    migrated_url, min_size=1, max_size=4, kwargs={"options": "-c lock_timeout=10ms"}
                ) as pool,
            ):
                runner = loop.Reconciler(pool, FakeReaper(), config=make_reconcile_config())
                counts = []
                for _ in range(2):
                    report = await runner.run_once()
                    assert report.failures == ()
                    counts.append(report.repair_counts[lane])
                assert counts == [0, 1], "a persistent first page hid the healthy successor again"
                await _assert_repaired(conn, lane, 101)

    asyncio.run(run())


_FIELDS = {
    "leaked_mutation_obligations": "leaked_mutation_after",
    "stalled_reprovisioning_systems": "stalled_reprovision_after",
}
_LIMITS = {
    "leaked_mutation_obligations": "_LEAKED_MUTATION_REPAIR_LIMIT",
    "stalled_reprovisioning_systems": "_STALLED_REPROVISIONING_REPAIR_LIMIT",
}


async def _repair(
    conn: psycopg.AsyncConnection, lane: Lane, progress: repairs.SystemRepairProgress
) -> int:
    if lane == "leaked_mutation_obligations":
        return await repairs.repair_leaked_mutation_obligations(conn, progress=progress)
    return await repairs.repair_stalled_reprovisioning_systems(conn, progress=progress)


def test_runners_sharing_config_and_pool_keep_independent_progress(
    migrated_url: str, lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repairs, _LIMITS[lane], 2)

    async def run() -> None:
        async with await connect(migrated_url) as conn, await connect(migrated_url) as blocker:
            await _seed(conn, lane, range(1, 4))
            async with (
                _hold_locks(blocker, (1, 2)),
                AsyncConnectionPool(
                    migrated_url, min_size=1, max_size=4, kwargs={"options": "-c lock_timeout=10ms"}
                ) as pool,
            ):
                config = make_reconcile_config()
                first = loop.Reconciler(pool, FakeReaper(), config=config)
                peer = loop.Reconciler(pool, FakeReaper(), config=config)
                counts = [
                    (await runner.run_once()).repair_counts[lane] for runner in (first, peer, first)
                ]
                assert counts == [0, 0, 1], "a peer inherited another runner's cursor"
                await _assert_repaired(conn, lane, 3)

    asyncio.run(run())


@pytest.mark.parametrize("limit", [1, 2])
def test_partial_tail_wraps_to_retry_earlier_failures_and_resets_only_its_hint(
    migrated_url: str, lane: Lane, limit: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repairs, _LIMITS[lane], limit)

    async def run() -> None:
        other_field = next(field for kind, field in _FIELDS.items() if kind != lane)
        progress = repairs.SystemRepairProgress(after=UUID(int=777))
        setattr(progress, other_field, UUID(int=555))
        async with await connect(migrated_url) as conn, await connect(migrated_url) as blocker:
            await _seed(conn, lane, range(limit + 1))
            await conn.execute("SET lock_timeout='10ms'")
            async with _hold_locks(blocker, range(limit)):
                assert await _repair(conn, lane, progress) == 0
                assert getattr(progress, _FIELDS[lane]) == UUID(int=limit - 1)
                assert await _repair(conn, lane, progress) == 1
                assert getattr(progress, _FIELDS[lane]) == UUID(int=limit)
            assert await _repair(conn, lane, progress) == limit
            assert getattr(progress, _FIELDS[lane]) == UUID(int=limit - 1)
            for index in range(limit + 1):
                await _assert_repaired(conn, lane, index)
            assert await _repair(conn, lane, progress) == 0
            assert getattr(progress, _FIELDS[lane]) is None
            assert progress.after == UUID(int=777)
            assert getattr(progress, other_field) == UUID(int=555)

    asyncio.run(run())


def test_two_lanes_share_a_carrier_without_sharing_a_position(migrated_url: str) -> None:
    async def run() -> None:
        progress = repairs.SystemRepairProgress(after=UUID(int=777))
        async with await connect(migrated_url) as conn:
            await _seed(conn, "leaked_mutation_obligations", (10,))
            await _seed(conn, "stalled_reprovisioning_systems", (1, 20))
            assert await _repair(conn, "leaked_mutation_obligations", progress) == 1
            assert await _repair(conn, "stalled_reprovisioning_systems", progress) == 2
            await _assert_repaired(conn, "leaked_mutation_obligations", 10)
            await _assert_repaired(conn, "stalled_reprovisioning_systems", 1)
            await _assert_repaired(conn, "stalled_reprovisioning_systems", 20)
            assert progress.leaked_mutation_after == UUID(int=10)
            assert progress.stalled_reprovision_after == UUID(int=20)
            assert progress.after == UUID(int=777)

    asyncio.run(run())


@pytest.mark.parametrize("wrapped", [False, True])
def test_failed_selection_preserves_progress_and_leaves_connection_idle(
    migrated_url: str, lane: Lane, wrapped: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        from psycopg.pq import TransactionStatus

        progress = repairs.SystemRepairProgress()
        before = UUID(int=10)
        setattr(progress, _FIELDS[lane], before)
        selector_name = (
            "_leaked_mutation_candidates"
            if lane == "leaked_mutation_obligations"
            else "_stalled_reprovision_candidates"
        )
        original = getattr(repairs, selector_name)
        async with await connect(migrated_url) as conn, await connect(migrated_url) as blocker:
            await conn.execute("SET lock_timeout='20ms'")
            async with blocker.transaction():
                if wrapped:

                    async def lock_after_empty_tail(
                        connection: psycopg.AsyncConnection, *, after: UUID | None = None
                    ) -> list[UUID]:
                        result = await original(connection, after=after)
                        if after is not None:
                            assert result == []
                            await blocker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
                        return result

                    monkeypatch.setattr(repairs, selector_name, lock_after_empty_tail)
                else:
                    await blocker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await _repair(conn, lane, progress)
            monkeypatch.setattr(repairs, selector_name, original)
            assert getattr(progress, _FIELDS[lane]) == before
            assert conn.info.transaction_status == TransactionStatus.IDLE
            assert await _repair(conn, lane, progress) == 0
            assert getattr(progress, _FIELDS[lane]) is None

    asyncio.run(run())


@pytest.mark.parametrize("wrapped", [False, True])
def test_cancellation_retries_the_interrupted_system(
    migrated_url: str, lane: Lane, wrapped: bool
) -> None:
    async def run() -> None:
        from psycopg.pq import TransactionStatus

        progress = repairs.SystemRepairProgress()
        setattr(progress, _FIELDS[lane], UUID(int=10) if wrapped else UUID(int=0))
        task: asyncio.Task[int] | None = None
        async with await connect(migrated_url) as conn, await connect(migrated_url) as blocker:
            await _seed(conn, lane, (1, 2))
            try:
                async with _hold_locks(blocker, (2,)):
                    task = asyncio.create_task(_repair(conn, lane, progress))
                    async with asyncio.timeout(10):
                        while True:
                            row = await (
                                await blocker.execute(
                                    "SELECT EXISTS(SELECT 1 FROM pg_locks "
                                    "WHERE pid=%s AND locktype='advisory' AND NOT granted)",
                                    (conn.info.backend_pid,),
                                )
                            ).fetchone()
                            if row == (True,):
                                break
                    assert getattr(progress, _FIELDS[lane]) == UUID(int=1)
                    await _assert_repaired(blocker, lane, 1)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=10)
            finally:
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert getattr(progress, _FIELDS[lane]) == UUID(int=1)
            assert conn.info.transaction_status == TransactionStatus.IDLE
            assert await _repair(conn, lane, progress) == 1
            await _assert_repaired(conn, lane, 2)
            assert getattr(progress, _FIELDS[lane]) == UUID(int=2)

    asyncio.run(run())


def test_guarded_skip_advances_after_a_real_job_arrives_under_the_lock(
    migrated_url: str, lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.reconciler.recovery_support import seed_aged_teardown_job as _seed_teardown_job
    from tests.reconciler.recovery_support import seed_reprovision_job as _seed_job

    monkeypatch.setattr(repairs, _LIMITS[lane], 1)
    original_lock = repairs.advisory_xact_lock

    async def run() -> None:
        progress = repairs.SystemRepairProgress()

        @asynccontextmanager
        async def racing_lock(
            conn: psycopg.AsyncConnection, scope: LockScope, key: UUID | str
        ) -> AsyncGenerator[None]:
            async with original_lock(conn, scope, key):
                if key == UUID(int=0):
                    async with await connect(migrated_url) as racer:
                        if lane == "leaked_mutation_obligations":
                            await _seed_teardown_job(racer, UUID(int=0), state="running")
                        else:
                            await _seed_job(racer, UUID(int=0), state="running")
                yield

        monkeypatch.setattr(repairs, "advisory_xact_lock", racing_lock)
        async with await connect(migrated_url) as conn:
            await _seed(conn, lane, (0, 1))
            assert await _repair(conn, lane, progress) == 0
            assert getattr(progress, _FIELDS[lane]) == UUID(int=0)
            assert await _repair(conn, lane, progress) == 1
            await _assert_repaired(conn, lane, 1)
            assert getattr(progress, _FIELDS[lane]) == UUID(int=1)

    asyncio.run(run())


def test_empty_tail_wraps_once_and_the_empty_population_resets(
    migrated_url: str, lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    selector_name = (
        "_leaked_mutation_candidates"
        if lane == "leaked_mutation_obligations"
        else "_stalled_reprovision_candidates"
    )
    original = getattr(repairs, selector_name)
    seen: list[UUID | None] = []

    async def traced(conn: psycopg.AsyncConnection, *, after: UUID | None = None) -> list[UUID]:
        seen.append(after)
        return await original(conn, after=after)

    monkeypatch.setattr(repairs, selector_name, traced)

    async def run() -> None:
        progress = repairs.SystemRepairProgress()
        setattr(progress, _FIELDS[lane], UUID(int=10))
        async with await connect(migrated_url) as conn:
            assert await _repair(conn, lane, progress) == 0
            assert seen == [UUID(int=10), None]
            assert getattr(progress, _FIELDS[lane]) is None
            seen.clear()
            assert await _repair(conn, lane, progress) == 0
            assert seen == [None]

    asyncio.run(run())
