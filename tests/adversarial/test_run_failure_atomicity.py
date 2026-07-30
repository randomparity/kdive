"""Adversarial: a Run's terminal transition cannot be separated from its job's dead-letter.

The worker's failure path makes two writes — ``queue.fail`` (the job's dead-letter or requeue)
and the owning Run's ``failed`` transition. Before ADR-0500 they ran as **two transactions on
the same connection**, so anything landing between them left a job durably ``failed`` beside a
Run still in ``created``/``running``.

That state is unreachable by every repair the platform has, which is what makes it worse than
the Systems window ADR-0492 closed:

- ``repair_abandoned_jobs`` selects ``jobs WHERE state = 'running' AND lease_expires_at < now()
  AND attempt >= max_attempts``; an already-``failed`` job never matches.
- ``queue.dequeue`` claims only ``queued`` or lapsed-``running`` rows, so the reclaim path
  cannot re-derive the failure either.
- No other pass transitions a Run: ``worker.py`` and ``reconciler/repairs/jobs.py`` are the only
  writers of ``RunState.FAILED``, and no sweep is keyed on Run age.

So the Run stayed non-terminal **forever**. ADR-0492's Consequences described this residual as
leaving "a Run without its category"; the shape is worse than that — the whole transition is
lost, not just the column.

Two windows are pinned, one per recovery route the fix relies on:

1. **The Run's own ``UPDATE`` faults** (real Postgres contention: a concurrent
   ``SELECT … FOR UPDATE`` on the ``runs`` row while the worker runs under ``lock_timeout``).
   Attempts are exhausted, so ``repair_abandoned_jobs`` is the reaper.
2. **The worker is torn down in the gap** — ``queue.fail`` returns and nothing else runs.
   Attempts remain, so ``queue.dequeue``'s reclaim is the reaper, and the retry re-derives the
   handler's real category.

Neither needs a process death to be *reachable* in production: the exception escapes
``_run_handler``'s ``except`` into ``_claim_loop``, which catches ``Exception`` and continues.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import JobState, RunState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import Authorizing, InstallPayload
from kdive.jobs.worker import Worker
from kdive.reconciler.repairs.jobs import repair_abandoned_jobs
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.integration._seed import (
    seed_granted_allocation,
    seed_running_run,
    seed_system,
)

_AUTHORIZING = Authorizing(principal="p", agent_session=None, project="proj")
_WORKER = "w1"
_RECLAIMER = "w2"
_REASON = "kernel config rejects the requested feature"
# Non-retryable, so ADR-0483 dead-letters on the *first* attempt of three — the window is
# reachable with attempts still remaining, where the Systems twin needed them exhausted.
_CATEGORY = ErrorCategory.CONFIGURATION_ERROR


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool whose every connection waits at most 250 ms for a lock, then errors.

    ``lock_timeout`` arrives as a libpq connection option rather than a ``SET``, because a
    ``SET`` on a pooled non-autocommit connection is transactional and the pool's rollback
    would undo it.
    """
    pool = AsyncConnectionPool(
        url,
        kwargs={"options": "-c lock_timeout=250ms"},
        min_size=2,
        max_size=6,
        open=False,
    )
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


@asynccontextmanager
async def _run_row_locked(url: str, run_id: str) -> AsyncIterator[None]:
    """Hold a row lock on ``runs`` for the block, from a connection outside the pool.

    This is the fault: the Run's ``UPDATE`` blocks and the worker's ``lock_timeout`` turns the
    wait into a real ``LockNotAvailable``. A row lock rather than the Run's *advisory* lock on
    purpose — an advisory-lock holder would stall the worker before ``queue.fail``, which is the
    benign ordering. Faulting the second write is what forces atomicity to carry the outcome.
    """
    async with await psycopg.AsyncConnection.connect(url) as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM runs WHERE id = %s FOR UPDATE", (run_id,))
        yield


async def _seed_run(pool: AsyncConnectionPool) -> str:
    allocation_id = await seed_granted_allocation(pool)
    system_id = await seed_system(pool, allocation_id, SystemState.READY)
    return await seed_running_run(pool, system_id)


def _worker(pool: AsyncConnectionPool, registry: HandlerRegistry) -> Worker:
    return Worker(pool, registry, worker_id=_WORKER, secret_registry=SecretRegistry())


async def _enqueue(
    pool: AsyncConnectionPool, run_id: str, dedup_key: str, *, max_attempts: int
) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.INSTALL,
            InstallPayload(run_id=run_id),
            _AUTHORIZING,
            dedup_key,
            max_attempts=max_attempts,
        )


def _raises_terminal_error() -> HandlerRegistry:
    async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
        raise CategorizedError(_REASON, category=_CATEGORY)

    registry = HandlerRegistry()
    registry.register(JobKind.INSTALL, handler)
    return registry


async def _job_row(pool: AsyncConnectionPool, job_id: UUID) -> dict[str, Any]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT state, error_category, worker_id, attempt FROM jobs WHERE id = %s",
            (job_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return row


async def _run_row(pool: AsyncConnectionPool, run_id: str) -> dict[str, Any]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT state, failure_category, failing_job_id FROM runs WHERE id = %s",
            (run_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return row


async def _lapse_lease(pool: AsyncConnectionPool, job_id: UUID) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 hour' WHERE id = %s",
            (job_id,),
        )


def test_faulting_the_run_transition_leaves_the_job_reapable(migrated_url: str) -> None:
    """Window 1: the Run's own ``UPDATE`` faults, so neither write may stand.

    The assertion that bites is the **job's** state. Before ADR-0500 ``queue.fail`` had already
    committed, so the job was durably ``failed`` — outside ``repair_abandoned_jobs``' ``state =
    'running'`` predicate and outside ``dequeue``'s, which is why the Run was orphaned with no
    reaper. Rolling the pair back restores the one state the platform already sweeps, and the
    sweep is then run for real rather than reasoned about.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool)
            job = await _enqueue(pool, run_id, f"{run_id}:install:fault", max_attempts=1)
            worker = _worker(pool, _raises_terminal_error())

            async with _run_row_locked(migrated_url, run_id):
                # `_claim_loop` swallows this in production; no process death is needed.
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await worker.run_once()

            job_row = await _job_row(pool, job.id)
            assert job_row["state"] == JobState.RUNNING.value
            assert job_row["error_category"] is None
            run_row = await _run_row(pool, run_id)
            assert run_row["state"] == RunState.RUNNING.value
            assert run_row["failure_category"] is None

            # Reapable: attempt 1 of max_attempts 1, so the lapsed lease makes it a zombie the
            # reconciler dead-letters — and that sweep transitions the Run in the same pass.
            await _lapse_lease(pool, job.id)
            async with pool.connection() as conn:
                assert await repair_abandoned_jobs(conn) == 1
            assert await _run_row(pool, run_id) == {
                "state": RunState.FAILED.value,
                "failure_category": ErrorCategory.LEASE_EXPIRED.value,
                # The reconciler's sweep records no pointer; only the worker's write does.
                "failing_job_id": None,
            }

    asyncio.run(_run())


def test_worker_torn_down_in_the_gap_loses_neither_write(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Window 2: the worker dies the instant ``queue.fail`` returns, with attempts remaining.

    The real ``queue.fail`` runs and then a ``BaseException`` is raised in the gap — exactly what
    a torn-down process leaves the connection in, and a class ``_run_handler``'s ``except
    Exception`` cannot intercept. Attempts remain, so ``dequeue``'s reclaim is the reaper here
    and the retry re-derives the handler's own category: the Run ends ``failed`` with
    ``configuration_error``, not with the reconciler's ``lease_expired``.
    """
    real_fail = queue.fail

    async def fail_then_die(*args: Any, **kwargs: Any) -> Job:
        await real_fail(*args, **kwargs)
        raise asyncio.CancelledError("worker torn down between the two writes")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool)
            job = await _enqueue(pool, run_id, f"{run_id}:install:torndown", max_attempts=3)
            worker = _worker(pool, _raises_terminal_error())

            monkeypatch.setattr(queue, "fail", fail_then_die)
            with pytest.raises(asyncio.CancelledError):
                await worker.run_once()
            monkeypatch.undo()

            job_row = await _job_row(pool, job.id)
            assert job_row["state"] == JobState.RUNNING.value
            assert job_row["error_category"] is None
            assert await _run_row(pool, run_id) == {
                "state": RunState.RUNNING.value,
                "failure_category": None,
                "failing_job_id": None,
            }

            # Reapable by the queue itself: the lapsed lease is reclaimable while attempts
            # remain, and this attempt finalizes both writes together.
            await _lapse_lease(pool, job.id)
            reclaimed = await worker.run_once()
            assert reclaimed is not None and reclaimed.id == job.id
            assert (await _job_row(pool, job.id))["attempt"] == 2
            assert await _run_row(pool, run_id) == {
                "state": RunState.FAILED.value,
                "failure_category": _CATEGORY.value,
                "failing_job_id": job.id,
            }

    asyncio.run(_run())


def test_unknown_kind_dead_letter_and_run_transition_are_one_unit(migrated_url: str) -> None:
    """The second call site — ``run_once``'s no-handler arm — is atomic too.

    ``ErrorCategory.NOT_IMPLEMENTED`` dead-letters a job whose kind has no registered handler
    (``worker.py``'s ``run_once``). It reached the Run through the same split pair, and the issue
    names it; without this it would be the one path still able to orphan a Run.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool)
            job = await _enqueue(pool, run_id, f"{run_id}:install:nohandler", max_attempts=1)
            worker = _worker(pool, HandlerRegistry())  # no handlers registered

            async with _run_row_locked(migrated_url, run_id):
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await worker.run_once()

            job_row = await _job_row(pool, job.id)
            assert job_row["state"] == JobState.RUNNING.value
            assert job_row["error_category"] is None
            assert (await _run_row(pool, run_id))["state"] == RunState.RUNNING.value

            await _lapse_lease(pool, job.id)
            async with pool.connection() as conn:
                assert await repair_abandoned_jobs(conn) == 1
            assert (await _run_row(pool, run_id))["state"] == RunState.FAILED.value

    asyncio.run(_run())


def test_stale_worker_that_lost_its_lease_does_not_fail_the_run(migrated_url: str) -> None:
    """A regression guard on the fence, not a loss shape: it holds before and after ADR-0500.

    Merging the two writes must not let a reclaimed job's stale worker transition the Run.
    ``queue.fail``'s ``worker_id`` fence misses, so it returns the job still ``running``, and the
    Run write is skipped — inside the shared transaction exactly as it was outside it. The race
    is real: the lease lapses mid-handler and the second worker claims the job while the first is
    still running it.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool)
            job = await _enqueue(pool, run_id, f"{run_id}:install:stale", max_attempts=3)

            async def handler(conn: psycopg.AsyncConnection, claimed: Job) -> str:
                await _lapse_lease(pool, claimed.id)
                async with pool.connection() as other:
                    reclaimed = await queue.dequeue(other, _RECLAIMER)
                assert reclaimed is not None and reclaimed.id == claimed.id
                raise CategorizedError(_REASON, category=_CATEGORY)

            registry = HandlerRegistry()
            registry.register(JobKind.INSTALL, handler)
            await _worker(pool, registry).run_once()

            job_row = await _job_row(pool, job.id)
            assert job_row["worker_id"] == _RECLAIMER
            assert job_row["state"] == JobState.RUNNING.value
            assert job_row["error_category"] is None
            assert await _run_row(pool, run_id) == {
                "state": RunState.RUNNING.value,
                "failure_category": None,
                "failing_job_id": None,
            }

    asyncio.run(_run())


def test_worker_takes_the_run_lock_before_queue_fail_writes_anything(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock order: RUN advisory **then** the ``jobs`` row, matching every other RUN writer.

    ``runs.boot``/``runs.install`` hold ``LockScope.RUN`` and then row-lock this very job via
    ``queue.enqueue``'s ``recycle_terminal`` ``UPDATE … WHERE dedup_key = %s``. Spanning the two
    worker writes in one transaction co-holds locks the old shape released between them, so
    row-locking the job first and *then* waiting on that advisory lock would be an ABBA deadlock
    against that caller.

    Holding the Run's advisory lock from outside must therefore stall the worker before
    ``queue.fail`` runs at all. Asserting the job's *state* would not discriminate — a rollback
    leaves it ``running`` either way — so the assertion is that ``queue.fail`` was never reached.
    """
    calls: list[UUID] = []
    real_fail = queue.fail

    async def recording_fail(conn: Any, job: Job, *args: Any, **kwargs: Any) -> Job:
        calls.append(job.id)
        return await real_fail(conn, job, *args, **kwargs)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool)
            job = await _enqueue(pool, run_id, f"{run_id}:install:lockorder", max_attempts=3)
            worker = _worker(pool, _raises_terminal_error())
            monkeypatch.setattr(queue, "fail", recording_fail)

            async with (
                await psycopg.AsyncConnection.connect(migrated_url) as holder,
                holder.transaction(),
                advisory_xact_lock(holder, LockScope.RUN, UUID(run_id)),
            ):
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await worker.run_once()

            assert calls == [], "the RUN lock must be held before queue.fail writes anything"
            job_row = await _job_row(pool, job.id)
            assert job_row["state"] == JobState.RUNNING.value
            assert job_row["error_category"] is None

    asyncio.run(_run())
