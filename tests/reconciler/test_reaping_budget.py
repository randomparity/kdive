"""The per-lane pass budget on the two host-state reaping lanes (ADR-0565, #1980).

The load-bearing tests here are the two ``holds_its_lock_until_the_provider_call_finishes`` cases.
They are the proof #1980 asks for: that the bound never ends a transaction while a provider call
may still be mutating host state. The shape is deliberate and each part carries weight —

* the fake reaper runs its work inside ``asyncio.shield``, so cancelling the coroutine awaiting it
  does **not** stop the work. That is the asymmetry a real reaper has: it drives a synchronous
  libvirt client through ``asyncio.to_thread``, and a cancelled await abandons the worker thread
  rather than stopping it;
* the work sleeps well past the budget before observing anything, so the observation is taken long
  after any competing rollback would have completed — not raced against it;
* the observation is taken from a **second** connection, because a lock's holder cannot ask whether
  it still holds it. ``pg_try_advisory_xact_lock`` on the same key returns false while the lane
  holds it and true once the lane's transaction has ended.

Rewrite the budget as an ``asyncio.timeout`` around the provider call and the lane's transaction
unwinds while the shielded work is still running, so the observation flips to "not held" and both
tests redden. That is the whole point of them.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import LiteralString
from uuid import UUID, uuid4

import psycopg
import pytest

import kdive.config as config
from kdive.config.core_settings import RECONCILER_LANE_BUDGET_SECONDS
from kdive.db.locks import CAPTURE_JOB_FENCE_KEY_SQL, LockScope, _lock_key
from kdive.domain.errors import CategorizedError
from kdive.providers.infra.reaping import (
    CaptureReaper,
    DumpVolume,
    DumpVolumeReaper,
    OrphanedCapture,
)
from kdive.reconciler.cleanup.provider_reaping import (
    DEFAULT_CAPTURE_REAP_BATCH,
    DEFAULT_CAPTURE_RETRY_BASE,
    DEFAULT_CAPTURE_RETRY_CAP,
    DEFAULT_LANE_BUDGET,
    reap_orphaned_captures,
    reap_orphaned_dump_volumes,
)
from tests.reconciler.capture_reaping_support import (
    REMOTE,
    SETTLE,
    connect,
    reap_state,
    seed_capture_job,
    seed_chain,
)

_GRACE = timedelta(minutes=30)

#: Short enough that the first provider call always outlives it, long enough that the *first*
#: candidate is never gated out by scheduler jitter before any work has happened.
_BUDGET = timedelta(seconds=0.2)
#: An order of magnitude past the budget, so an observation taken after it cannot race a rollback
#: that a cancelled await would have triggered at the budget.
_CALL_SECONDS = 2.0


async def _lock_is_held(url: str, sql: LiteralString, key: UUID | int) -> bool:
    """Whether some other transaction holds ``key``, asked from a fresh connection.

    A holder cannot answer this about itself: ``pg_try_advisory_xact_lock`` is re-entrant within a
    session, so the lane's own connection would always report the lock free to take.
    """
    async with await psycopg.AsyncConnection.connect(url) as probe, probe.transaction():
        cursor = await probe.execute(f"SELECT pg_try_advisory_xact_lock({sql})", (key,))  # noqa: S608
        row = await cursor.fetchone()
        assert row is not None
        return not row[0]


class _ShieldedCaptureReaper:
    """A capture reaper whose work outlives a cancellation of the coroutine awaiting it."""

    def __init__(self, url: str, *, seconds: float = _CALL_SECONDS) -> None:
        self._url = url
        self._seconds = seconds
        self.seen: list[OrphanedCapture] = []
        self.fence_held_at_end: list[bool] = []
        self.tasks: list[asyncio.Task[bool]] = []

    async def reclaim_capture(self, capture: OrphanedCapture) -> bool:
        self.seen.append(capture)
        task = asyncio.create_task(self._work(capture.job_id))
        self.tasks.append(task)
        return await asyncio.shield(task)

    async def _work(self, job_id: UUID) -> bool:
        await asyncio.sleep(self._seconds)
        self.fence_held_at_end.append(
            await _lock_is_held(self._url, CAPTURE_JOB_FENCE_KEY_SQL, job_id)
        )
        return True

    async def drain(self) -> None:
        """Let abandoned work finish, so its observation is recorded before the test asserts."""
        for task in self.tasks:
            await asyncio.gather(task, return_exceptions=True)


class _ShieldedDumpVolumeReaper:
    """A dump-volume reaper whose delete outlives a cancellation of the awaiting coroutine."""

    def __init__(
        self,
        url: str,
        volumes: list[DumpVolume],
        *,
        seconds: float = _CALL_SECONDS,
        list_seconds: float = 0.0,
    ) -> None:
        self._url = url
        self._volumes = volumes
        self._seconds = seconds
        self._list_seconds = list_seconds
        self.deleted: list[str] = []
        self.lock_held_at_end: list[bool] = []
        self.tasks: list[asyncio.Task[bool]] = []

    async def list_dump_volumes(self) -> list[DumpVolume]:
        if self._list_seconds:
            await asyncio.sleep(self._list_seconds)
        return list(self._volumes)

    async def delete_dump_volume(self, name: str, *, expected_mtime_epoch_s: float) -> bool:
        del expected_mtime_epoch_s
        self.deleted.append(name)
        system_id = next(v.system_id for v in self._volumes if v.name == name)
        assert system_id is not None
        task = asyncio.create_task(self._work(system_id))
        self.tasks.append(task)
        return await asyncio.shield(task)

    async def _work(self, system_id: UUID) -> bool:
        await asyncio.sleep(self._seconds)
        self.lock_held_at_end.append(
            await _lock_is_held(self._url, "%s", _lock_key(LockScope.SYSTEM, system_id))
        )
        return True

    async def drain(self) -> None:
        for task in self.tasks:
            await asyncio.gather(task, return_exceptions=True)


async def _reap_captures(
    url: str, reapers: dict[str, CaptureReaper], *, budget: timedelta = DEFAULT_LANE_BUDGET
) -> int:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        return await reap_orphaned_captures(
            conn,
            reapers,
            settle=SETTLE,
            batch=DEFAULT_CAPTURE_REAP_BATCH,
            retry_base=DEFAULT_CAPTURE_RETRY_BASE,
            retry_cap=DEFAULT_CAPTURE_RETRY_CAP,
            budget=budget,
        )


async def _reap_volumes(
    url: str, reaper: DumpVolumeReaper, *, budget: timedelta = DEFAULT_LANE_BUDGET
) -> int:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        return await reap_orphaned_dump_volumes(conn, reaper, _GRACE, budget=budget)


def _orphan_volume(system_id: UUID) -> DumpVolume:
    """A volume old enough to clear the grace window and named so its System parses out."""
    return DumpVolume(
        name=f"kdive-host-dump-{system_id}.kdump", system_id=system_id, mtime_epoch_s=1.0
    )


# --- R2: the bound never ends a transaction mid-mutation -----------------------------------------


def test_capture_lane_holds_its_fence_until_the_provider_call_finishes(migrated_url: str) -> None:
    reaper = _ShieldedCaptureReaper(migrated_url)

    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            _, run_id = await seed_chain(conn, domain_name="kdive-stored")
            job_id = await seed_capture_job(conn, run_id)

            reaped = await _reap_captures(migrated_url, {REMOTE: reaper}, budget=_BUDGET)
            await reaper.drain()

            # The evidence: the ownership fence was still held when the provider call finished its
            # work, long after the budget expired. An asyncio.timeout rewrite releases it first.
            assert reaper.fence_held_at_end == [True]
            # Corroborating, but not sufficient on its own: _dispatch_capture catches Exception, so
            # a TimeoutError would read as a decline and defer rather than as a lost fence.
            assert reaped == 1
            assert await reap_state(conn, job_id) == (1, False, True)

    asyncio.run(_run())


def test_dump_volume_lane_holds_its_system_lock_until_the_delete_finishes(
    migrated_url: str,
) -> None:
    system_id = uuid4()
    reaper = _ShieldedDumpVolumeReaper(migrated_url, [_orphan_volume(system_id)])

    async def _run() -> None:
        reaped = await _reap_volumes(migrated_url, reaper, budget=_BUDGET)
        await reaper.drain()

        assert reaper.lock_held_at_end == [True]
        assert reaped == 1

    asyncio.run(_run())


# --- the budget gates between candidates, never during one ---------------------------------------


def test_capture_lane_stops_dispatching_once_the_budget_is_spent(migrated_url: str) -> None:
    reaper = _ShieldedCaptureReaper(migrated_url, seconds=_CALL_SECONDS)

    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            job_ids = []
            for _ in range(3):
                _, run_id = await seed_chain(conn, domain_name="kdive-stored")
                job_ids.append(await seed_capture_job(conn, run_id))

            assert await _reap_captures(migrated_url, {REMOTE: reaper}, budget=_BUDGET) == 1
            await reaper.drain()

            # Exactly one provider call: the first candidate outlived the budget, and the check
            # before candidate two stopped the lane.
            assert len(reaper.seen) == 1
            dispatched = reaper.seen[0].job_id
            untouched = [j for j in job_ids if j != dispatched]
            # A candidate the budget never reached writes no deferral, so it keeps its place in the
            # untouched leading group and the next pass reaches it first.
            for job_id in untouched:
                assert await reap_state(conn, job_id) is None

    asyncio.run(_run())


def test_dump_volume_lane_stops_deleting_once_the_budget_is_spent(migrated_url: str) -> None:
    volumes = [_orphan_volume(uuid4()) for _ in range(3)]
    reaper = _ShieldedDumpVolumeReaper(migrated_url, volumes)

    async def _run() -> None:
        assert await _reap_volumes(migrated_url, reaper, budget=_BUDGET) == 1
        await reaper.drain()
        assert reaper.deleted == [volumes[0].name]

    asyncio.run(_run())


def test_a_budget_that_is_not_spent_attempts_every_candidate(migrated_url: str) -> None:
    volumes = [_orphan_volume(uuid4()) for _ in range(3)]
    reaper = _ShieldedDumpVolumeReaper(migrated_url, volumes, seconds=0.0)

    async def _run() -> None:
        assert await _reap_volumes(migrated_url, reaper, budget=timedelta(seconds=30)) == 3
        await reaper.drain()
        assert reaper.deleted == [v.name for v in volumes]

    asyncio.run(_run())


def test_a_slow_candidate_listing_does_not_starve_the_dump_volume_lane(migrated_url: str) -> None:
    """The listing runs before the deadline starts, or the lane livelocks at zero per pass.

    ``list_dump_volumes`` is itself a whole-fleet fan-out costing up to one connect timeout per
    declared host. A deadline started at the top of the lane would already be spent by the time the
    loop began on any fleet with more down hosts than ``budget / connect_timeout``, and the lane
    would then reclaim nothing, on every pass, forever.
    """
    volumes = [_orphan_volume(uuid4())]
    reaper = _ShieldedDumpVolumeReaper(
        migrated_url, volumes, seconds=0.0, list_seconds=_BUDGET.total_seconds() * 3
    )

    async def _run() -> None:
        assert await _reap_volumes(migrated_url, reaper, budget=_BUDGET) == 1
        assert reaper.deleted == [volumes[0].name]

    asyncio.run(_run())


# --- the operator-facing bound --------------------------------------------------------------------


def test_the_budget_setting_default_matches_the_lane_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KDIVE_RECONCILER_LANE_BUDGET_SECONDS", raising=False)
    config.load()
    assert timedelta(seconds=config.require(RECONCILER_LANE_BUDGET_SECONDS)) == DEFAULT_LANE_BUDGET


@pytest.mark.parametrize("raw", ["0", "-5"])
def test_a_non_positive_budget_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """A budget of zero would attempt nothing — a switch that disables both sweeps in silence."""
    monkeypatch.setenv("KDIVE_RECONCILER_LANE_BUDGET_SECONDS", raw)
    config.load()
    with pytest.raises(CategorizedError) as excinfo:
        config.require(RECONCILER_LANE_BUDGET_SECONDS)
    assert "KDIVE_RECONCILER_LANE_BUDGET_SECONDS" in str(excinfo.value)
