"""Per-lane claim loops in one worker process (#1538, ADR-0550).

The point of the change: a job claimed on one lane must not delay a claim on another. The
pre-ADR-0550 worker ran a single serial claim loop, so a `restore` sat behind whatever long
`image_build` the one lane was running, with its System fenced `RESTORING` for the duration.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import (
    DEFAULT_JOB_DISPATCH_LANE,
    STATE_FENCED_JOB_DISPATCH_LANE,
    Job,
    JobKind,
)
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import Authorizing, InstallPayload, RestorePayload
from kdive.jobs.worker import Worker, WorkerConfig
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs.worker_incarnations import CURRENT_WORKER_FENCE_PROTOCOL

_AUTHORIZING = Authorizing(principal="p", agent_session=None, project="a")
_INCARNATION_CREDENTIAL = SecretStr("worker-lane-test-credential")
_BOTH_LANES = (DEFAULT_JOB_DISPATCH_LANE, STATE_FENCED_JOB_DISPATCH_LANE)


def _unopened_pool(max_size: int = 8) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        "postgresql://localhost/unused", min_size=1, max_size=max_size, open=False
    )


def _worker(pool: AsyncConnectionPool, registry: HandlerRegistry, **kwargs: Any) -> Worker:
    kwargs.setdefault("incarnation_credential", _INCARNATION_CREDENTIAL)
    kwargs.setdefault("secret_registry", SecretRegistry())
    return Worker(pool, registry, **kwargs)


async def _registered_worker(
    pool: AsyncConnectionPool, registry: HandlerRegistry, **kwargs: Any
) -> Worker:
    worker_id = cast(str, kwargs["worker_id"])
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
            "fence_protocol, credential_hash) VALUES "
            "(%s, 'local', '{}'::jsonb, %s, sha256(convert_to(%s, 'UTF8'))) "
            "ON CONFLICT (incarnation) DO NOTHING",
            (worker_id, CURRENT_WORKER_FENCE_PROTOCOL, _INCARNATION_CREDENTIAL.get_secret_value()),
        )
    return _worker(pool, registry, **kwargs)


async def _job_state(url: str, job_id: UUID) -> JobState:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        cur = await conn.execute("SELECT state FROM jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None
    return JobState(row[0])


class _LoopDied(BaseException):
    """A failure outside ``_claim_loop``'s ``except Exception`` — the loop task actually ends."""


def _restore_payload() -> RestorePayload:
    return RestorePayload(system_id=str(uuid4()), name="snap", start_paused=False)


# --------------------------------------------------------------------------------------
# S6 — the pool floor now scales with the lane count, plus the readiness probe's connection.
# --------------------------------------------------------------------------------------


def test_pool_floor_scales_with_the_lane_count() -> None:
    # Two lanes dispatching hold 2 connections each (handler + heartbeat); the readiness probe
    # shares this pool and needs one more, and `run_once` skips `dequeue` while not ready — so a
    # worker sized to exactly 2*lanes stops claiming precisely when it is busiest.
    with pytest.raises(ValueError, match="max_size"):
        _worker(
            _unopened_pool(max_size=4),
            HandlerRegistry(),
            worker_id="w1",
            config=WorkerConfig(accepted_lanes=_BOTH_LANES),
        )
    # Exactly at the floor constructs.
    _worker(
        _unopened_pool(max_size=5),
        HandlerRegistry(),
        worker_id="w1",
        config=WorkerConfig(accepted_lanes=_BOTH_LANES),
    )


def test_a_single_lane_worker_keeps_the_old_floor() -> None:
    with pytest.raises(ValueError, match="max_size"):
        _worker(
            _unopened_pool(max_size=2),
            HandlerRegistry(),
            worker_id="w1",
            config=WorkerConfig(accepted_lanes=(DEFAULT_JOB_DISPATCH_LANE,)),
        )
    _worker(
        _unopened_pool(max_size=3),
        HandlerRegistry(),
        worker_id="w1",
        config=WorkerConfig(accepted_lanes=(DEFAULT_JOB_DISPATCH_LANE,)),
    )


def test_the_floor_message_names_the_lane_count(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ValueError) as excinfo:
        _worker(
            _unopened_pool(max_size=4),
            HandlerRegistry(),
            worker_id="w1",
            config=WorkerConfig(accepted_lanes=_BOTH_LANES),
        )
    message = str(excinfo.value)
    assert "4" in message and "5" in message
    assert "lane" in message


# --------------------------------------------------------------------------------------
# S10 — narrowing the lane set is supported, and says so.
# --------------------------------------------------------------------------------------


def test_a_worker_omitting_a_routed_lane_warns_at_construction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S10. Narrowing is legitimate, so this warns rather than refusing to start — but the
    omitted lane's jobs are then never claimed, and nothing else would surface that."""
    with caplog.at_level(logging.WARNING, logger="kdive.jobs.worker"):
        _worker(
            _unopened_pool(max_size=3),
            HandlerRegistry(),
            worker_id="w1",
            config=WorkerConfig(accepted_lanes=(DEFAULT_JOB_DISPATCH_LANE,)),
        )
    assert any(STATE_FENCED_JOB_DISPATCH_LANE in record.getMessage() for record in caplog.records)


def test_a_worker_accepting_every_routed_lane_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="kdive.jobs.worker"):
        _worker(
            _unopened_pool(max_size=5),
            HandlerRegistry(),
            worker_id="w1",
            config=WorkerConfig(accepted_lanes=_BOTH_LANES),
        )
    assert not [record for record in caplog.records if "lane" in record.getMessage()]


# --------------------------------------------------------------------------------------
# S4 — a worker claims only its own lanes.
# --------------------------------------------------------------------------------------


def test_a_fenced_lane_worker_leaves_a_default_job_queued(migrated_url: str) -> None:
    """S4."""

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            reg = HandlerRegistry()
            reg.register(JobKind.INSTALL, lambda conn, job: _noop())
            worker = await _registered_worker(
                pool,
                reg,
                worker_id="w-fenced-only",
                config=WorkerConfig(accepted_lanes=(STATE_FENCED_JOB_DISPATCH_LANE,)),
            )
            async with pool.connection() as conn:
                install = await queue.enqueue(
                    conn,
                    JobKind.INSTALL,
                    InstallPayload(run_id=str(uuid4())),
                    _AUTHORIZING,
                    "dk-lane-s4",
                )

            claimed = await worker.run_once(STATE_FENCED_JOB_DISPATCH_LANE)

            assert claimed is None
            assert await _job_state(migrated_url, install.id) is JobState.QUEUED

    asyncio.run(_run())


async def _noop() -> str:
    return "s3://out"


async def _await_state(pool: AsyncConnectionPool, job_id: UUID, want: JobState) -> None:
    """Poll until ``job_id`` reaches ``want``; the caller bounds this with `wait_for`."""
    while True:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT state FROM jobs WHERE id = %s", (job_id,))
            row = await cur.fetchone()
        if row is not None and JobState(row[0]) is want:
            return
        await asyncio.sleep(0.02)


# --------------------------------------------------------------------------------------
# S3 — the criterion that proves the reported defect is fixed.
# --------------------------------------------------------------------------------------


def test_a_running_default_job_does_not_delay_a_fenced_claim(migrated_url: str) -> None:
    """S3. The whole point of ADR-0550.

    A `default` handler is held mid-flight on an event that is only set *after* the fenced job
    has run to completion. On the pre-ADR-0550 single loop this deadlocks: the one loop is inside
    the install handler and can never reach the restore. Asserting on both jobs reaching their
    own terminal state — not merely on the fenced job being claimed — is what makes this bite.
    """

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=12) as pool:
            fenced_done = asyncio.Event()
            order: list[str] = []

            async def install_handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                order.append("install:start")
                # Blocks until the fenced job has finished, so the fenced claim cannot be
                # explained by this handler having already returned.
                await asyncio.wait_for(fenced_done.wait(), timeout=10)
                order.append("install:end")
                return "s3://install"

            async def restore_handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                order.append("restore:ran")
                fenced_done.set()
                return "s3://restore"

            reg = HandlerRegistry()
            reg.register(JobKind.INSTALL, install_handler)
            reg.register(JobKind.RESTORE, restore_handler)
            worker = await _registered_worker(
                pool,
                reg,
                worker_id="w-two-lane",
                config=WorkerConfig(
                    accepted_lanes=_BOTH_LANES, poll_interval=timedelta(milliseconds=10)
                ),
            )

            async with pool.connection() as conn:
                install = await queue.enqueue(
                    conn,
                    JobKind.INSTALL,
                    InstallPayload(run_id=str(uuid4())),
                    _AUTHORIZING,
                    "dk-lane-s3-install",
                )
                restore = await queue.enqueue(
                    conn, JobKind.RESTORE, _restore_payload(), _AUTHORIZING, "dk-lane-s3-restore"
                )
            assert install.dispatch_lane == DEFAULT_JOB_DISPATCH_LANE
            assert restore.dispatch_lane == STATE_FENCED_JOB_DISPATCH_LANE

            # Drive `run`, not `run_once`: the behaviour under test is that `run` gives each
            # accepted lane its own loop. A test that called `run_once(lane)` twice itself would
            # pass on a single-loop worker too, because it would be supplying the concurrency the
            # worker is supposed to provide.
            stop = asyncio.Event()
            runner = asyncio.create_task(worker.run(stop))
            try:
                await asyncio.wait_for(fenced_done.wait(), timeout=15)
                await asyncio.wait_for(_await_state(pool, install.id, JobState.SUCCEEDED), 15)
            finally:
                stop.set()
                await asyncio.wait_for(runner, timeout=15)

            # The install handler cannot return until the restore handler has run, so a single
            # serial claim loop deadlocks and the waits above expire. Reaching here is the result.
            assert order.index("restore:ran") < order.index("install:end")
            assert await _job_state(migrated_url, restore.id) is JobState.SUCCEEDED
            assert await _job_state(migrated_url, install.id) is JobState.SUCCEEDED

    asyncio.run(_run())


# --------------------------------------------------------------------------------------
# Supervision — a worker must never quietly serve fewer lanes than it advertises.
# --------------------------------------------------------------------------------------


def test_a_loop_dying_while_stop_is_unset_ends_the_worker(migrated_url: str) -> None:
    """A partial worker is the starvation case arriving by a different route.

    `asyncio.gather` propagates the first exception but leaves its siblings *running*, so
    without explicit cancellation the surviving loop would be orphaned behind a `run` that has
    already returned.
    """

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=12) as pool:
            worker = await _registered_worker(
                pool,
                HandlerRegistry(),
                worker_id="w-supervise",
                config=WorkerConfig(accepted_lanes=_BOTH_LANES),
            )
            stop = asyncio.Event()
            calls: list[str] = []

            async def exploding_run_once(lane: str) -> Job | None:
                calls.append(lane)
                if lane == STATE_FENCED_JOB_DISPATCH_LANE:
                    # Outside `_claim_loop`'s `except Exception`, so the loop task really ends —
                    # the only way a loop stops short of the stop event.
                    raise _LoopDied(lane)
                await asyncio.sleep(0.05)
                return None

            # Stand in for a loop dying: `ty` rejects the bound/unbound signature mismatch.
            worker.run_once = exploding_run_once  # ty: ignore[invalid-assignment]

            # Must return rather than hang: the surviving `default` loop is cancelled.
            with pytest.raises(_LoopDied):
                await asyncio.wait_for(worker.run(stop), timeout=10)
            assert STATE_FENCED_JOB_DISPATCH_LANE in calls

    asyncio.run(_run())


def test_both_loops_stop_on_the_shared_stop_event(migrated_url: str) -> None:
    """The normal shutdown path with every lane idle: `run` returns rather than hanging."""

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=12) as pool:
            worker = await _registered_worker(
                pool,
                HandlerRegistry(),
                worker_id="w-stop",
                config=WorkerConfig(
                    accepted_lanes=_BOTH_LANES, poll_interval=timedelta(milliseconds=10)
                ),
            )
            stop = asyncio.Event()
            task = asyncio.create_task(worker.run(stop))
            await asyncio.sleep(0.2)
            stop.set()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(_run())


def test_shutdown_drains_a_job_still_running_on_another_lane(migrated_url: str) -> None:
    """Shutdown must not abort work a lane has not yet had the chance to notice `stop` for.

    A claim loop re-reads `stop` only at the top of an iteration, so a loop inside a handler —
    a kernel build, a memory snapshot — is unaware of it for as long as that handler runs. The
    idle lane exits at once; if the busy lane were cancelled with it, the handler would be torn
    down with its lease still held, leaving the row `running` until the lease lapsed and the job
    was reclaimed and re-run. The single-loop worker drained on shutdown; so must this.

    The idle-lane version of this test cannot catch that: with nothing queued, every loop exits
    promptly and no loop is ever pending-and-busy.
    """

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=12) as pool:
            entered = asyncio.Event()
            release = asyncio.Event()

            async def slow_handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=10)
                return "s3://slow"

            reg = HandlerRegistry()
            reg.register(JobKind.RESTORE, slow_handler)
            worker = await _registered_worker(
                pool,
                reg,
                worker_id="w-drain",
                config=WorkerConfig(
                    accepted_lanes=_BOTH_LANES, poll_interval=timedelta(milliseconds=10)
                ),
            )
            async with pool.connection() as conn:
                restore = await queue.enqueue(
                    conn, JobKind.RESTORE, _restore_payload(), _AUTHORIZING, "dk-lane-drain"
                )

            stop = asyncio.Event()
            runner = asyncio.create_task(worker.run(stop))
            await asyncio.wait_for(entered.wait(), timeout=10)  # fenced lane is mid-handler

            stop.set()  # the idle `default` loop exits immediately
            await asyncio.sleep(0.2)
            assert not runner.done(), "run returned while a lane was still inside its handler"

            release.set()
            await asyncio.wait_for(runner, timeout=10)

            assert await _job_state(migrated_url, restore.id) is JobState.SUCCEEDED

    asyncio.run(_run())
