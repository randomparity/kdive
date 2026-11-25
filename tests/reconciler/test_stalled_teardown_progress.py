"""A skipped stalled-System page must yield to later ordinary teardown work."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal
from uuid import UUID

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import JobState, SystemState
from kdive.reconciler import loop
from kdive.reconciler.repairs import systems as system_repairs
from tests.reconcile_helpers import make_reconcile_config
from tests.reconciler.conftest import FakeReaper, connect, seed_system
from tests.reconciler.recovery_support import (
    seed_completed_activation as _seed_completed_activation,
)
from tests.reconciler.recovery_support import seed_teardown_job as _seed_teardown_job
from tests.reconciler.recovery_support import teardown_job as _teardown_job

type Blocker = Literal["authority_system_v1", "external_boot_authority_v1", "unsettled_activation"]
_KIND = "stalled_tearing_down_systems"


async def _seed_candidate(
    conn: psycopg.AsyncConnection, system_id: UUID, blocker: Blocker | None = None
) -> None:
    seeded = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
    await conn.execute("UPDATE systems SET id=%s WHERE id=%s", (system_id, seeded))
    if blocker is None:
        return
    await _seed_teardown_job(conn, system_id, state=JobState.FAILED)
    if blocker == "unsettled_activation":
        activation_id = await _seed_completed_activation(conn, system_id)
        assert not await ExternalBootActivationRepository().is_settled(conn, activation_id)
    else:
        await conn.execute(
            "UPDATE jobs SET payload=payload || %s WHERE dedup_key=%s",
            (Jsonb({blocker: {"marker": 1}}), f"{system_id}:teardown"),
        )


async def _seed_blocked_page(url: str, blocker: Blocker) -> UUID:
    async with await connect(url) as conn, conn.transaction():
        for index in range(1, 101):
            await _seed_candidate(conn, UUID(int=index), blocker)
        healthy = UUID(int=101)
        await _seed_candidate(conn, healthy)
    return healthy


@pytest.mark.parametrize(
    "blocker", ["authority_system_v1", "external_boot_authority_v1", "unsettled_activation"]
)
def test_a_full_skipped_page_yields_to_a_healthy_successor(
    migrated_url: str, blocker: Blocker
) -> None:
    async def run() -> None:
        healthy = await _seed_blocked_page(migrated_url, blocker)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            runner = loop.Reconciler(pool, FakeReaper(), config=make_reconcile_config())
            counts = []
            for _ in range(2):
                counts.append((await runner.run_once()).repair_counts[_KIND])
        assert counts == [0, 1], "a full skipped page hid the healthy successor again"
        async with await connect(migrated_url) as conn:
            assert await _teardown_job(conn, healthy) == ("queued", 0)
            row = await (
                await conn.execute("SELECT count(*) FROM jobs WHERE state='failed'")
            ).fetchone()
            assert row is not None and row[0] == 100

    asyncio.run(run())


def test_a_healthy_candidate_is_admitted_without_a_skipped_prefix(migrated_url: str) -> None:
    async def run() -> None:
        healthy = UUID(int=101)
        async with await connect(migrated_url) as conn:
            await _seed_candidate(conn, healthy)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            runner = loop.Reconciler(pool, FakeReaper(), config=make_reconcile_config())
            counts = []
            for _ in range(2):
                counts.append((await runner.run_once()).repair_counts[_KIND])
        assert counts == [1, 0]
        async with await connect(migrated_url) as conn:
            assert await _teardown_job(conn, healthy) == ("queued", 0)

    asyncio.run(run())


def test_two_runners_sharing_config_and_pool_keep_independent_page_progress(
    migrated_url: str,
) -> None:
    async def run() -> None:
        healthy = await _seed_blocked_page(migrated_url, "authority_system_v1")
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            config = make_reconcile_config()
            first = loop.Reconciler(pool, FakeReaper(), config=config)
            peer = loop.Reconciler(pool, FakeReaper(), config=config)
            counts = []
            for runner in (first, peer, first):
                counts.append((await runner.run_once()).repair_counts[_KIND])
        assert counts == [0, 0, 1], "another runner inherited page progress"
        async with await connect(migrated_url) as conn:
            assert await _teardown_job(conn, healthy) == ("queued", 0)

    asyncio.run(run())


def test_wrap_revisits_an_early_candidate_that_becomes_ordinary(migrated_url: str) -> None:
    async def run() -> None:
        healthy = await _seed_blocked_page(migrated_url, "authority_system_v1")
        progress = system_repairs.StalledTeardownProgress()
        history = system_repairs.TeardownWarningHistory()
        async with await connect(migrated_url) as conn:
            assert (
                await system_repairs.repair_stalled_tearing_down_systems(
                    conn, warnings=history, progress=progress
                )
                == 0
            )
            assert len(history.stalled) == 100
            assert (
                await system_repairs.repair_stalled_tearing_down_systems(
                    conn, warnings=history, progress=progress
                )
                == 1
            )
            assert progress.after == healthy and history.stalled == {}
            early = UUID(int=1)
            await conn.execute(
                "UPDATE jobs SET payload=payload - 'authority_system_v1' WHERE dedup_key=%s",
                (f"{early}:teardown",),
            )
            assert (
                await system_repairs.repair_stalled_tearing_down_systems(
                    conn, warnings=history, progress=progress
                )
                == 1
            )
            assert await _teardown_job(conn, early) == ("queued", 0)
            assert progress.after == UUID(int=100)
            assert set(history.stalled) == {UUID(int=i) for i in range(2, 101)}

    asyncio.run(run())


def test_a_partial_tail_is_not_filled_with_another_page(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_repairs, "_STALLED_TEARING_DOWN_REPAIR_LIMIT", 2)

    async def run() -> None:
        progress = system_repairs.StalledTeardownProgress()
        history = system_repairs.TeardownWarningHistory()
        async with await connect(migrated_url) as conn:
            for index in range(1, 4):
                await _seed_candidate(conn, UUID(int=index), "authority_system_v1")
            observations = []
            for _ in range(3):
                assert (
                    await system_repairs.repair_stalled_tearing_down_systems(
                        conn, warnings=history, progress=progress
                    )
                    == 0
                )
                observations.append((progress.after, set(history.stalled)))
        assert observations == [
            (UUID(int=2), {UUID(int=1), UUID(int=2)}),
            (UUID(int=3), {UUID(int=3)}),
            (UUID(int=2), {UUID(int=1), UUID(int=2)}),
        ]

    asyncio.run(run())


def test_uuid_zero_is_a_real_candidate_and_checkpoint(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_repairs, "_STALLED_TEARING_DOWN_REPAIR_LIMIT", 1)

    async def run() -> None:
        progress = system_repairs.StalledTeardownProgress()
        async with await connect(migrated_url) as conn:
            await _seed_candidate(conn, UUID(int=0), "authority_system_v1")
            await _seed_candidate(conn, UUID(int=1))
            assert (
                await system_repairs.repair_stalled_tearing_down_systems(conn, progress=progress)
                == 0
            )
            assert progress.after == UUID(int=0)
            assert (
                await system_repairs.repair_stalled_tearing_down_systems(conn, progress=progress)
                == 1
            )
            assert progress.after == UUID(int=1)
            assert await _teardown_job(conn, UUID(int=1)) == ("queued", 0)

    asyncio.run(run())


def test_empty_tail_wraps_once_then_resets_progress_and_current_warning_page(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[UUID | None] = []
    original = system_repairs._stalled_teardown_candidates

    async def traced(conn: psycopg.AsyncConnection, *, after: UUID | None = None) -> list[UUID]:
        seen.append(after)
        return await original(conn, after=after)

    monkeypatch.setattr(system_repairs, "_stalled_teardown_candidates", traced)

    async def run() -> None:
        after = UUID(int=10)
        progress = system_repairs.StalledTeardownProgress(after=after)
        history = system_repairs.TeardownWarningHistory(
            stalled={UUID(int=1): "authority_system_marked"}
        )
        async with await connect(migrated_url) as conn:
            assert (
                await system_repairs.repair_stalled_tearing_down_systems(
                    conn, progress=progress, warnings=history
                )
                == 0
            )
        assert seen == [after, None]
        assert progress.after is None and history.stalled == {}

    asyncio.run(run())


def test_a_full_page_of_real_lock_timeouts_yields_to_the_unlocked_successor(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)

    async def run() -> None:
        progress = system_repairs.StalledTeardownProgress()
        async with await connect(migrated_url) as conn, await connect(migrated_url) as blocker:
            for index in range(1, 102):
                await _seed_candidate(conn, UUID(int=index))
            await conn.execute("SET lock_timeout='10ms'")
            async with blocker.transaction():
                # Transaction-scoped locks remain held after these individual contexts exit.
                for index in range(1, 101):
                    async with advisory_xact_lock(blocker, LockScope.SYSTEM, UUID(int=index)):
                        pass
                assert (
                    await system_repairs.repair_stalled_tearing_down_systems(
                        conn, progress=progress
                    )
                    == 0
                )
                assert progress.after == UUID(int=100)
                assert conn.info.transaction_status == TransactionStatus.IDLE
                assert (
                    await system_repairs.repair_stalled_tearing_down_systems(
                        conn, progress=progress
                    )
                    == 1
                )
            assert await _teardown_job(conn, UUID(int=101)) == ("queued", 0)
        assert (
            sum("stalled teardown replay failed" in r.getMessage() for r in caplog.records) == 100
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("blocked_index", "initial_hint"),
    [(1, UUID(int=0)), (2, UUID(int=0)), (1, UUID(int=3))],
    ids=["first", "second", "wrapped-first"],
)
def test_cancelled_system_lock_keeps_the_last_completed_checkpoint(
    migrated_url: str, blocked_index: int, initial_hint: UUID
) -> None:
    async def run() -> None:
        first, second = UUID(int=1), UUID(int=2)
        progress = system_repairs.StalledTeardownProgress(after=initial_hint)
        expected_hint = initial_hint if blocked_index == 1 else first
        first_job = None if blocked_index == 1 else ("queued", 0)
        task: asyncio.Task[int] | None = None
        async with await connect(migrated_url) as conn, await connect(migrated_url) as blocker:
            await _seed_candidate(conn, first)
            await _seed_candidate(conn, second)
            try:
                async with (
                    blocker.transaction(),
                    advisory_xact_lock(blocker, LockScope.SYSTEM, UUID(int=blocked_index)),
                ):
                    task = asyncio.create_task(
                        system_repairs.repair_stalled_tearing_down_systems(conn, progress=progress)
                    )
                    async with asyncio.timeout(10):
                        while True:
                            row = await (
                                await blocker.execute(
                                    "SELECT EXISTS(SELECT 1 FROM pg_locks "
                                    "WHERE pid=%s AND locktype='advisory' AND NOT granted)",
                                    (conn.info.backend_pid,),
                                )
                            ).fetchone()
                            if row is not None and row[0]:
                                break
                    assert progress.after == expected_hint
                    assert await _teardown_job(blocker, first) == first_job
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=10)
            finally:
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert progress.after == expected_hint
            assert conn.info.transaction_status == TransactionStatus.IDLE
            assert await _teardown_job(conn, first) == first_job
            assert await _teardown_job(conn, second) is None
            assert await system_repairs.repair_stalled_tearing_down_systems(
                conn, progress=progress
            ) == (2 if blocked_index == 1 else 1)
            assert progress.after == second
            assert await _teardown_job(conn, first) == ("queued", 0)
            assert await _teardown_job(conn, second) == ("queued", 0)

    asyncio.run(run())


def test_failed_initial_select_retains_progress_history_and_idle_connection(
    migrated_url: str,
) -> None:
    async def run() -> None:
        after, system_id = UUID(int=0), UUID(int=1)
        progress = system_repairs.StalledTeardownProgress(after=after)
        history = system_repairs.TeardownWarningHistory(
            stalled={system_id: "authority_system_marked"}
        )
        before = dict(history.stalled)
        async with await connect(migrated_url) as conn, await connect(migrated_url) as blocker:
            await _seed_candidate(conn, system_id, "authority_system_v1")
            await conn.execute("SET lock_timeout='50ms'")
            async with blocker.transaction():
                await blocker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await system_repairs.repair_stalled_tearing_down_systems(
                        conn, progress=progress, warnings=history
                    )
            assert progress.after == after and history.stalled == before
            assert conn.info.transaction_status == TransactionStatus.IDLE

    asyncio.run(run())
