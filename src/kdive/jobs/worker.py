"""The worker tier: claim, heartbeat, dispatch, finalize (ADR-0018, ADR-0550).

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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import JobState, RunState
from kdive.domain.errors import CategorizedError, ErrorCategory, retryable_category
from kdive.domain.operations.jobs import ACTIVE_JOB_KINDS, Job, JobKind, dispatch_lane_for_kind
from kdive.health.heartbeat import Heartbeat, tick_until_stop
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry, JobHandler
from kdive.jobs.payloads import PayloadValidationError, run_id_from_payload
from kdive.jobs.worker_telemetry import JobSpan, WorkerTelemetry
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)
_CONTEXT_VALUE_MAX = 1000
_CONTEXT_KEY = re.compile(r"[^a-zA-Z0-9_.-]+")
_RUN_COMPENSATION_STATES = (RunState.CREATED, RunState.RUNNING)
_RUN_COMPENSATION_STATE_VALUES = tuple(state.value for state in _RUN_COMPENSATION_STATES)


async def _sleep_until_stop(stop: asyncio.Event, timeout: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout)


def _routed_lanes() -> frozenset[str]:
    """Every dispatch lane an active job kind is admitted onto (ADR-0550)."""
    return frozenset(dispatch_lane_for_kind(kind) for kind in ACTIVE_JOB_KINDS)


def _warn_on_unconsumed_lanes(accepted_lanes: Sequence[str]) -> None:
    """Warn when this worker accepts a strict subset of the lanes kinds route to.

    Narrowing is supported — it restores the pre-ADR-0550 single-job-per-process footprint, and a
    deliberately split fleet is a shape the decision allows — so this warns rather than refusing
    to start. But jobs on an omitted lane are never claimed by *this* worker, and if no deployed
    worker accepts the lane they are never claimed at all: the System or Snapshot they fence stays
    fenced, and nothing surfaces it, because ``repair_abandoned_jobs`` reaps only ``running`` rows.
    This log line is the only signal that an operator chose that.
    """
    omitted = sorted(_routed_lanes() - set(accepted_lanes))
    if omitted:
        _log.warning(
            "worker accepts %s but job kinds also route to %s; jobs on those lanes are not "
            "claimed by this worker and stay queued unless another worker accepts them",
            ", ".join(accepted_lanes),
            ", ".join(omitted),
        )


DEFAULT_ACCEPTED_LANES: tuple[str, ...] = tuple(sorted(_routed_lanes()))
"""Every lane a job kind routes to — the safe default, since a lane with no consumer starves."""


def worker_pool_floor(accepted_lanes: Sequence[str]) -> int:
    """The smallest ``pool.max_size`` a worker accepting ``accepted_lanes`` can run on.

    Each lane dispatches one job at a time, and a dispatched job holds two connections at once —
    its handler's and its background heartbeat's. The ``+ 1`` is the readiness probe, which shares
    this pool: ``run_once`` skips ``dequeue`` while not ready, so a worker sized to exactly
    ``2 * lanes`` would stop claiming precisely when every lane is busy.

    A correctness floor, not a sizing recommendation — it leaves no headroom beyond that one
    probe. Exported so the process composition sizes its pool from the same formula the
    constructor enforces; two independent expressions of it would drift and the worker would
    raise at startup.
    """
    return 2 * len(accepted_lanes) + 1


@dataclass(frozen=True)
class WorkerConfig:
    """Timing, health, and telemetry collaborators for :class:`Worker`."""

    lease: timedelta = queue.DEFAULT_LEASE
    accepted_lanes: tuple[str, ...] = DEFAULT_ACCEPTED_LANES
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
        incarnation_credential: SecretStr,
        secret_registry: SecretRegistry,
        config: WorkerConfig = DEFAULT_WORKER_CONFIG,
    ) -> None:
        """Build a worker.

        Args:
            incarnation_credential: Authority-minted credential for ``worker_id``. Every claim
                authenticates it at the database boundary.
            config: Lease timing plus optional ``/livez`` heartbeat, readiness gate, and
                per-job telemetry. ``None`` values in the config disable the optional
                collaborators (always ready, no background liveness ticker, no-op telemetry).

        Raises:
            ValueError: ``heartbeat_interval > lease / 3`` — too coarse to keep the
                lease alive across a missed beat, which would let the job be reclaimed
                and double-run; ``accepted_lanes`` is empty, which would start no claim
                loop at all; or ``pool.max_size`` is below :func:`worker_pool_floor` —
                each accepted lane dispatches one job at a time holding its handler's
                connection and its background heartbeat's, and the readiness probe
                shares the pool, so a smaller pool would stall every dispatch until the
                heartbeat acquisition timed out.
        """
        if config.heartbeat_interval > config.lease / 3:
            raise ValueError(
                f"heartbeat_interval ({config.heartbeat_interval}) must be <= lease/3 "
                f"({config.lease / 3}); a coarser interval risks mid-job reclaim and double-run"
            )
        if not config.accepted_lanes:
            raise ValueError("accepted_lanes must name at least one dispatch lane")
        lanes = len(config.accepted_lanes)
        floor = worker_pool_floor(config.accepted_lanes)
        if pool.max_size < floor:
            raise ValueError(
                f"pool.max_size ({pool.max_size}) must be >= {floor} for {lanes} accepted "
                f"lane(s): each lane dispatches one job at a time holding its handler "
                "connection and a concurrent heartbeat connection, plus one for the readiness "
                "probe that shares this pool — and run_once skips dequeue while not ready, so a "
                "pool sized to exactly 2*lanes stops claiming under full dispatch"
            )
        _warn_on_unconsumed_lanes(config.accepted_lanes)
        self._pool = pool
        self._registry = registry
        self._worker_id = worker_id
        self._incarnation_credential = incarnation_credential
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
        self._stop_event: asyncio.Event | None = None

    async def run_once(self, lane: str) -> Job | None:
        """Claim and dispatch one job **from ``lane``**; return it, or ``None`` if idle.

        Scoped to a single lane because :meth:`run` gives each accepted lane its own loop
        (ADR-0550): claiming across the whole accepted set from one loop is what let a queued
        ``restore`` sit behind a running ``image_build`` with its System fenced.

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
            single_lane = (lane,)
            job = await queue.dequeue(
                conn,
                self._worker_id,
                incarnation_credential=self._incarnation_credential,
                lease=self._lease,
                accepted_lanes=single_lane,
            )
            if self._telemetry.enabled:
                self._telemetry.observe_queue_depth(
                    await queue.count_claimable(conn, accepted_lanes=single_lane), lane=lane
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
                    conn,
                    job,
                    ErrorCategory.NOT_IMPLEMENTED,
                    incarnation_credential=self._incarnation_credential,
                    terminal=True,
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
        self._stop_event = stop
        try:
            await self._run_lane_loops(stop)
        finally:
            self._stop_event = None
            if ticker is not None:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

    async def _run_lane_loops(self, stop: asyncio.Event) -> None:
        """Run one claim loop per accepted lane; drain on stop, cancel on an early death.

        The two exits are deliberately different, and conflating them aborts running work:

        - **``stop`` is set — the ordinary shutdown.** Every remaining loop is *awaited*, not
          cancelled. A loop only re-reads ``stop`` at the top of an iteration, so one that is
          inside a handler (a kernel build, a memory snapshot) has not noticed yet; cancelling it
          would tear the handler down with its lease still held, leaving the row ``running`` until
          the lease lapses and the job is reclaimed and re-run. The single-loop worker drained its
          job on shutdown and this keeps that contract, per lane.
        - **``stop`` is unset — a loop died early.** Every sibling is cancelled and the exception
          propagates, so the process supervisor restarts the worker rather than leaving it serving
          fewer lanes than it accepts, which is this decision's starvation case by another route.
          `asyncio.gather` alone would not do this: it propagates the first exception but leaves
          its siblings *running*, orphaned behind a ``run`` that has already returned.
        """
        loops = [
            asyncio.create_task(self._claim_loop(stop, lane), name=f"claim-loop:{lane}")
            for lane in self._accepted_lanes
        ]
        try:
            done, pending = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            if pending and not stop.is_set():
                _log.error(
                    "claim loop ended while the worker was still running; stopping the "
                    "remaining %d lane loop(s) so the process is restarted rather than "
                    "serving fewer lanes than it accepts",
                    len(pending),
                )
                for task in pending:
                    task.cancel()
            if pending:
                # On the stop path this waits out each lane's in-flight job; on the death path
                # the cancellations above have already landed.
                await asyncio.gather(*pending, return_exceptions=True)
            # Re-raise whatever ended a loop, after the siblings are down.
            for task in done:
                task.result()
        finally:
            for task in loops:
                if not task.done():
                    task.cancel()

    def _start_heartbeat_ticker(self, stop: asyncio.Event) -> asyncio.Task[None] | None:
        if self._heartbeat is None:
            return None
        return asyncio.create_task(
            tick_until_stop(
                self._heartbeat,
                stop,
                self._heartbeat_tick,
                self._heartbeat_sleep_until_stop,
            )
        )

    async def _claim_loop(self, stop: asyncio.Event, lane: str) -> None:
        poll = self._poll_interval.total_seconds()
        while not stop.is_set():
            try:
                job = await self.run_once(lane)
            except Exception:  # noqa: BLE001 - a durable worker survives a transient per-iteration error
                _log.exception("run_once failed on lane %s; continuing after %ss", lane, poll)
                await _sleep_until_stop(stop, poll)
                continue
            if job is None:
                await _sleep_until_stop(stop, poll)

    async def _dispatch(self, job: Job, handler: JobHandler) -> None:
        with self._telemetry.job_span(job.kind.value) as span:
            heartbeat = asyncio.create_task(self._heartbeat_loop(job.id, job.attempt))
            if job.kind is JobKind.CAPTURE_TRAFFIC:
                await self._dispatch_capture(job, handler, span, heartbeat)
                return
            try:
                await self._run_handler(job, handler, span)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _dispatch_capture(
        self,
        job: Job,
        handler: JobHandler,
        span: JobSpan,
        heartbeat: asyncio.Task[None],
    ) -> None:
        """Cancel a capture handler as soon as heartbeat or process authority ends."""
        handler_task = asyncio.create_task(self._run_handler(job, handler, span))
        stop_task = (
            asyncio.create_task(self._stop_event.wait()) if self._stop_event is not None else None
        )
        authority_tasks = {heartbeat}
        if stop_task is not None:
            authority_tasks.add(stop_task)
        try:
            done, _pending = await asyncio.wait(
                {handler_task, *authority_tasks}, return_when=asyncio.FIRST_COMPLETED
            )
            if handler_task in done:
                await handler_task
                return
            handler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handler_task
            if heartbeat in done:
                with contextlib.suppress(Exception):
                    heartbeat.result()
        finally:
            if not handler_task.done():
                handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)
            for task in authority_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*authority_tasks, return_exceptions=True)

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
                    incarnation_credential=self._incarnation_credential,
                    terminal=terminal,
                    failure_context=_failure_context(exc, self._secret_registry),
                )
            self._telemetry.record_job_failure(failed_job, category)
            if failed_job.state is JobState.QUEUED:
                self._telemetry.record_job_retry(job.kind.value)
            _log.warning("job %s failed: %s", job.id, category, exc_info=True)
            return
        async with self._pool.connection() as conn:
            completed = await queue.complete(
                conn,
                job.id,
                result_ref,
                attempt=job.attempt,
                incarnation_credential=self._incarnation_credential,
            )
        if completed is None:
            _log.warning("job %s completed but was reclaimed; result dropped", job.id)

    async def _heartbeat_loop(self, job_id: UUID, attempt: int) -> None:
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
                    if not await queue.heartbeat(
                        conn,
                        job_id,
                        attempt=attempt,
                        incarnation_credential=self._incarnation_credential,
                        lease=self._lease,
                    ):
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
    incarnation_credential: SecretStr,
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
        incarnation_credential: Authority for this worker incarnation.
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
            conn,
            job,
            category,
            incarnation_credential=incarnation_credential,
            terminal=terminal,
            failure_context=failure_context,
        )
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        failed_job = await queue.fail(
            conn,
            job,
            category,
            incarnation_credential=incarnation_credential,
            terminal=terminal,
            failure_context=failure_context,
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
