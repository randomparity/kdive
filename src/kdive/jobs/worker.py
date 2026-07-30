"""The worker tier: claim, heartbeat, dispatch, finalize (ADR-0018).

A :class:`Worker` owns an ``AsyncConnectionPool`` and processes one job per
:meth:`Worker.run_once`: ``dequeue`` claims and charges an attempt, a background
heartbeat renews the lease on a second connection, the registered handler runs on a
dispatch connection, and ``complete``/:func:`_fail_job_and_run` finalize on fresh
connections (so a handler that poisoned its connection cannot block finalization). The
worker holds no transaction across the handler — a handler runs 30+ minutes and commits
its own steps (ADR-0018 decision 7).

The failure path finalizes both of its writes — the job's dead-letter/requeue and the
owning Run's terminal transition — in **one** transaction on that fresh connection
(ADR-0500). That is not a transaction across the handler: it opens after the handler has
returned and its dispatch connection has been released.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import JobState, RunState
from kdive.domain.errors import CategorizedError, ErrorCategory, retryable_category
from kdive.domain.operations.jobs import Job
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry, JobHandler
from kdive.jobs.payloads import PayloadValidationError, run_id_from_payload
from kdive.jobs.worker_telemetry import JobSpan, WorkerTelemetry
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

if TYPE_CHECKING:
    from kdive.health.heartbeat import Heartbeat

_log = logging.getLogger(__name__)
_CONTEXT_VALUE_MAX = 1000
_CONTEXT_KEY = re.compile(r"[^a-zA-Z0-9_.-]+")
_RUN_COMPENSATION_STATES = (RunState.CREATED, RunState.RUNNING)
_RUN_COMPENSATION_STATE_VALUES = tuple(state.value for state in _RUN_COMPENSATION_STATES)


async def _sleep_until_stop(stop: asyncio.Event, timeout: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout)


@dataclass(frozen=True)
class WorkerConfig:
    """Timing, health, and telemetry collaborators for :class:`Worker`."""

    lease: timedelta = queue.DEFAULT_LEASE
    accepted_lanes: tuple[str, ...] = queue.DEFAULT_DISPATCH_LANES
    heartbeat_interval: timedelta = timedelta(seconds=30)
    poll_interval: timedelta = timedelta(seconds=1)
    heartbeat: Heartbeat | None = None
    heartbeat_tick: timedelta = timedelta(seconds=1)
    heartbeat_sleep_until_stop: Callable[[asyncio.Event, float], Awaitable[None]] = (
        _sleep_until_stop
    )
    readiness: Callable[[], Awaitable[bool]] | None = None
    telemetry: WorkerTelemetry | None = None


DEFAULT_WORKER_CONFIG = WorkerConfig()


class Worker:
    """Claims and dispatches durable jobs from the Postgres queue."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        registry: HandlerRegistry,
        *,
        worker_id: str,
        secret_registry: SecretRegistry,
        config: WorkerConfig = DEFAULT_WORKER_CONFIG,
    ) -> None:
        """Build a worker.

        Args:
            config: Lease timing plus optional ``/livez`` heartbeat, readiness gate, and
                per-job telemetry. ``None`` values in the config disable the optional
                collaborators (always ready, no background liveness ticker, no-op telemetry).

        Raises:
            ValueError: ``heartbeat_interval > lease / 3`` — too coarse to keep the
                lease alive across a missed beat, which would let the job be reclaimed
                and double-run; or ``pool.max_size < 2`` — a job in flight holds two
                connections at once (its handler's dispatch connection and the
                background heartbeat's), so a smaller pool would stall every dispatch
                until the heartbeat acquisition timed out.
        """
        if config.heartbeat_interval > config.lease / 3:
            raise ValueError(
                f"heartbeat_interval ({config.heartbeat_interval}) must be <= lease/3 "
                f"({config.lease / 3}); a coarser interval risks mid-job reclaim and double-run"
            )
        if pool.max_size < 2:
            raise ValueError(
                f"pool.max_size ({pool.max_size}) must be >= 2: a dispatched job holds "
                "its handler connection and a concurrent heartbeat connection at once"
            )
        self._pool = pool
        self._registry = registry
        self._worker_id = worker_id
        self._lease = config.lease
        self._accepted_lanes = config.accepted_lanes
        self._heartbeat_interval = config.heartbeat_interval
        self._poll_interval = config.poll_interval
        self._secret_registry = secret_registry
        self._heartbeat = config.heartbeat
        self._heartbeat_tick = config.heartbeat_tick.total_seconds()
        self._heartbeat_sleep_until_stop = config.heartbeat_sleep_until_stop
        self._readiness = config.readiness
        self._telemetry = config.telemetry or WorkerTelemetry.disabled()

    async def run_once(self) -> Job | None:
        """Claim and dispatch one job; return it, or ``None`` if idle.

        Skips ``dequeue`` and returns ``None`` (idle) when this process's backends are
        not reachable (ADR-0090 §5): a not-ready worker pauses dequeuing new jobs while a
        needed backend is down rather than failing them. It also reads ``queue_paused`` at
        the top of the claim loop (ADR-0062): while the queue is paused the worker skips
        ``dequeue`` too. In both cases a job already in flight in :meth:`_dispatch` is
        untouched and keeps heart-beating — the freeze applies only to the claim of *new*
        work; the reconciler keeps enqueuing, and those jobs simply wait for resume.
        """
        if not await self._is_ready():
            return None
        async with self._pool.connection() as conn:
            if await queue.is_queue_paused(conn):
                return None
            job = await queue.dequeue(
                conn, self._worker_id, lease=self._lease, accepted_lanes=self._accepted_lanes
            )
            if self._telemetry.enabled:
                self._telemetry.observe_queue_depth(
                    await queue.count_claimable(conn, accepted_lanes=self._accepted_lanes)
                )
        if job is None:
            return None
        if self._telemetry.enabled and job.heartbeat_at is not None and job.created_at is not None:
            self._telemetry.record_time_to_claim(
                job.kind.value, (job.heartbeat_at - job.created_at).total_seconds()
            )
        handler = self._registry.get(job.kind)
        if handler is None:
            async with self._pool.connection() as conn:
                failed_job = await _fail_job_and_run(
                    conn, job, ErrorCategory.NOT_IMPLEMENTED, terminal=True
                )
            self._telemetry.record_job_failure(failed_job, ErrorCategory.NOT_IMPLEMENTED)
            _log.warning("no handler for job %s kind %s; dead-lettered", job.id, job.kind)
            return job
        await self._dispatch(job, handler)
        return job

    async def _is_ready(self) -> bool:
        """Return readiness via the injected gate; always ready when no gate is wired."""
        if self._readiness is None:
            return True
        return await self._readiness()

    async def run(self, stop: asyncio.Event) -> None:
        """Loop :meth:`run_once`, sleeping ``poll_interval`` when idle or after an error.

        The ``/livez`` heartbeat is bumped by a **background ticker task** at
        :attr:`_heartbeat_tick` cadence (ADR-0090 §5), *not* per job — so a single
        long-running job (a kernel build runs for minutes, far past the stale bound) never
        starves the heartbeat and makes the worker read not-live. Liveness tracks the
        event loop, not the work unit: while the loop is scheduling the ticker keeps it
        live; a genuinely wedged event loop stops the ticker too and ``/livez`` goes stale.
        A stuck *job* (vs a stuck loop) is caught by job-duration metrics and the lease
        fence, not by liveness.

        A transient per-iteration error (e.g. a brief database outage in ``dequeue``)
        is logged and the loop continues — a durable worker must not die on one bad
        iteration. The sleep after an error avoids a hot error-loop while the
        dependency recovers.
        """
        ticker = self._start_heartbeat_ticker(stop)
        try:
            await self._claim_loop(stop)
        finally:
            if ticker is not None:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

    def _start_heartbeat_ticker(self, stop: asyncio.Event) -> asyncio.Task[None] | None:
        if self._heartbeat is None:
            return None
        return asyncio.create_task(
            _tick_until_stop(
                self._heartbeat,
                stop,
                self._heartbeat_tick,
                self._heartbeat_sleep_until_stop,
            )
        )

    async def _claim_loop(self, stop: asyncio.Event) -> None:
        poll = self._poll_interval.total_seconds()
        while not stop.is_set():
            try:
                job = await self.run_once()
            except Exception:  # noqa: BLE001 - a durable worker survives a transient per-iteration error
                _log.exception("run_once failed; continuing after %ss", poll)
                await _sleep_until_stop(stop, poll)
                continue
            if job is None:
                await _sleep_until_stop(stop, poll)

    async def _dispatch(self, job: Job, handler: JobHandler) -> None:
        with self._telemetry.job_span(job.kind.value) as span:
            heartbeat = asyncio.create_task(self._heartbeat_loop(job.id))
            try:
                await self._run_handler(job, handler, span)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _run_handler(self, job: Job, handler: JobHandler, span: JobSpan) -> None:
        try:
            async with self._pool.connection() as conn:
                await conn.set_autocommit(True)
                try:
                    result_ref = await handler(conn, job)
                finally:
                    await conn.set_autocommit(False)
        except Exception as exc:  # noqa: BLE001 - the worker turns any handler failure into a dead-letter/requeue
            span.set_outcome("error")
            category = _failure_category(exc)
            terminal = _is_terminal(exc, category)
            async with self._pool.connection() as conn:
                failed_job = await _fail_job_and_run(
                    conn,
                    job,
                    category,
                    terminal=terminal,
                    failure_context=_failure_context(exc, self._secret_registry),
                )
            self._telemetry.record_job_failure(failed_job, category)
            if failed_job.state is JobState.QUEUED:
                self._telemetry.record_job_retry(job.kind.value)
            _log.warning("job %s failed: %s", job.id, category, exc_info=True)
            return
        async with self._pool.connection() as conn:
            completed = await queue.complete(conn, job.id, self._worker_id, result_ref)
        if completed is None:
            _log.warning("job %s completed but was reclaimed; result dropped", job.id)

    async def _heartbeat_loop(self, job_id: UUID) -> None:
        """Renew the lease until cancelled, the fence misses, or a heartbeat errors.

        A failed heartbeat (DB blip, lost connection) is logged and ends the loop
        rather than escaping the task — the lease then lapses and the reconciler/
        next ``dequeue`` reclaims the job (the designed fallback). Letting it escape
        would re-raise out of ``_dispatch``'s ``finally`` and crash the worker.
        ``asyncio.CancelledError`` is a ``BaseException`` and so is not caught here,
        so normal cancellation still stops the loop.
        """
        interval = self._heartbeat_interval.total_seconds()
        try:
            async with self._pool.connection() as conn:
                while True:
                    await asyncio.sleep(interval)
                    if not await queue.heartbeat(conn, job_id, self._worker_id, lease=self._lease):
                        return
        except Exception:  # noqa: BLE001 - a failing heartbeat must not crash the worker; stop beating and let the lease lapse
            _log.warning(
                "heartbeat for job %s failed; stopping (lease will lapse)",
                job_id,
                exc_info=True,
            )


def _failure_category(exc: Exception) -> ErrorCategory:
    if isinstance(exc, CategorizedError):
        return exc.category
    if isinstance(exc, PayloadValidationError):
        return ErrorCategory.CONFIGURATION_ERROR
    return ErrorCategory.INFRASTRUCTURE_FAILURE


def _is_terminal(exc: Exception, category: ErrorCategory) -> bool:
    """Decide whether this failure dead-letters now or is re-dispatched for another attempt.

    The **category** is the primary signal (ADR-0483): a category the taxonomy calls
    non-retryable is permanent by construction — a denied guest-agent RPC, a malformed
    payload, a host binary that is not installed — so re-dispatching it can only reproduce
    the same failure, three times slower, under a category that already told the caller not
    to retry. Requiring every such raise site to remember ``terminal=True`` made the safe
    behaviour opt-in and it was widely missed (#1631).

    ``CategorizedError.terminal`` remains as the **escalation**: it forces an immediate
    dead-letter for a category that *is* retryable, where a retry would otherwise be
    reasonable but this particular failure already drove the target to a terminal state.

    Args:
        exc: The exception the handler raised.
        category: The category ``exc`` was classified as by :func:`_failure_category`.

    Returns:
        ``True`` to dead-letter immediately, ``False`` to requeue while attempts remain.
    """
    if isinstance(exc, CategorizedError) and exc.terminal:
        return True
    return not retryable_category(category)


async def _fail_job_and_run(
    conn: AsyncConnection,
    job: Job,
    category: ErrorCategory,
    *,
    terminal: bool,
    failure_context: Mapping[str, str] | None = None,
) -> Job:
    """Finalize a failed ``job`` and its owning Run as one transaction (ADR-0500).

    ``queue.fail`` dead-letters or requeues the job; :func:`_mark_run_failed` transitions the
    Run the payload names. Splitting those across two transactions is the #1684 defect: a
    dead-lettered job whose Run transition never landed leaves the Run in ``created``/
    ``running`` **permanently**, because the worker is the only writer of ``RunState.FAILED``
    besides ``repair_abandoned_jobs``, and that sweep selects on ``jobs.state = 'running'`` —
    which an already-``failed`` job can never satisfy. Committing them together means a
    torn-down worker or a faulting statement rolls **both** back, leaving the job ``running``
    with its lease still set: the state ``dequeue`` reclaims and the reconciler sweeps.

    The Run's advisory lock is taken **before** ``queue.fail``, not after it, because
    ``runs.boot``/``runs.install`` hold ``LockScope.RUN`` and then row-lock this very job
    (``queue.enqueue``'s ``recycle_terminal`` ``UPDATE`` on ``dedup_key = f"{run_id}:{step}"``).
    Row-locking the job first and then waiting on that lock would be an ABBA deadlock; this
    order matches every other RUN-scoped writer. Non-run-bearing kinds take no lock and open no
    outer transaction — ``queue.fail`` self-commits as it does for every other caller.

    Args:
        conn: A fresh finalize connection, not the handler's (ADR-0018 decision 7).
        job: The claimed job as this worker last saw it; its ``worker_id`` is the fence.
        category: The failure's category, recorded on both rows.
        terminal: Dead-letter now rather than requeue (see :func:`_is_terminal`).
        failure_context: Redacted context for the job row; ignored on a requeue.

    Returns:
        The job's post-write state, or the unchanged ``job`` when ``queue.fail``'s
        ``worker_id`` fence missed — in which case the Run is left alone, because a worker
        that lost its lease must not fail the Run another worker now owns.
    """
    run_id = _compensation_run_id(job)
    if run_id is None:
        return await queue.fail(
            conn, job, category, terminal=terminal, failure_context=failure_context
        )
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        failed_job = await queue.fail(
            conn, job, category, terminal=terminal, failure_context=failure_context
        )
        await _mark_run_failed(conn, failed_job, category, run_id)
        return failed_job


def _compensation_run_id(job: Job) -> UUID | None:
    """Return the Run this job's failure transitions, or ``None`` when there is none.

    ``None`` covers both a kind that carries no ``run_id`` and a persisted payload that no
    longer validates — the latter is logged rather than raised, so a malformed payload
    degrades to "the job is finalized, the Run is not reached" instead of escaping the
    ``except`` block and leaving the job unfinalized too.
    """
    try:
        return run_id_from_payload(job.kind, job.payload)
    except PayloadValidationError as exc:
        _log.warning(
            "job %s has invalid payload; skipping Run compensation: %s",
            job.id,
            exc,
        )
        return None


async def _mark_run_failed(
    conn: AsyncConnection, job: Job, category: ErrorCategory, run_id: UUID
) -> None:
    """Transition ``run_id`` to ``failed`` with ``category``, if ``job`` dead-lettered.

    Assumes the caller holds the transaction and the Run's advisory lock
    (:func:`_fail_job_and_run`). A requeued job — or one whose ``worker_id`` fence missed, which
    ``queue.fail`` reports by returning it still ``running`` — leaves the Run untouched. The
    ``state = ANY(...)`` guard keeps an already-terminal Run terminal.
    """
    if job.state is not JobState.FAILED:
        return
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE runs SET state = %s, failure_category = %s, failing_job_id = %s "
            "WHERE id = %s AND state = ANY(%s) "
            "RETURNING id",
            (
                RunState.FAILED.value,
                category.value,
                job.id,
                run_id,
                list(_RUN_COMPENSATION_STATE_VALUES),
            ),
        )
        row = await cur.fetchone()
    if row is not None:
        _log.info("job %s terminal failure compensated run %s", job.id, run_id)


def _failure_context(exc: Exception, registry: SecretRegistry) -> dict[str, str]:
    redactor = Redactor(registry=registry)
    context = {"failure_message": _redacted(redactor, str(exc))}
    if isinstance(exc, CategorizedError):
        for key, value in exc.details.items():
            if _safe_detail(value):
                context[f"failure_detail_{_context_key(str(key))}"] = _redacted(
                    redactor, "" if value is None else str(value)
                )
    return context


def _safe_detail(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool | UUID)


def _context_key(key: str) -> str:
    cleaned = _CONTEXT_KEY.sub("_", key).strip("_.-")
    return cleaned or "value"


def _redacted(redactor: Redactor, value: str) -> str:
    return redactor.redact_text(value)[:_CONTEXT_VALUE_MAX]


async def _tick_until_stop(
    heartbeat: Heartbeat,
    stop: asyncio.Event,
    interval: float,
    sleep_until_stop: Callable[[asyncio.Event, float], Awaitable[None]] = _sleep_until_stop,
) -> None:
    """Bump ``heartbeat`` every ``interval`` seconds until ``stop`` is set or cancelled.

    Runs concurrently with the claim loop so a long-running job never starves the
    ``/livez`` signal (ADR-0090 §5); a wedged event loop stops this ticker too, so a truly
    stuck worker still reads not-live.
    """
    heartbeat.tick()
    while not stop.is_set():
        await sleep_until_stop(stop, interval)
        if stop.is_set():
            break
        heartbeat.tick()
