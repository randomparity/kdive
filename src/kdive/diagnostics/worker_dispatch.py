"""Server-side bounded-wait dispatcher for the worker-vantage diagnostic checks (ADR-0164).

``ops.diagnostics`` runs in the server process; the worker-vantage checks must run on the worker
(ADR-0083), and the durable job queue is the only server->worker handoff. This dispatcher enqueues
a ``diagnostics_worker_check`` job and bounded-waits within ``WORKER_DISPATCH_BUDGET`` for its
inline result, keeping ``doctor``'s single coherent verdict (ADR-0091 §1). A job the worker never
picks up in time surfaces as ``WORKER_UNAVAILABLE`` (ADR-0139), never a hang.

``WORKER_UNAVAILABLE_DETAIL`` has one home — :mod:`kdive.diagnostics.service` — which this module
imports. The reverse dependency is broken at the source: ``service`` references the
:class:`WorkerCheckDispatcher` Protocol only under ``TYPE_CHECKING`` and imports
:class:`JobWorkerCheckDispatcher` function-locally, so importing ``service`` pulls in nothing here.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.checks import CheckResult, CheckStatus
from kdive.diagnostics.result_codec import ResultCodecError, deserialize_results
from kdive.diagnostics.service import WORKER_UNAVAILABLE_DETAIL
from kdive.domain.jobs import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue as job_queue
from kdive.jobs.payloads import Authorizing, DiagnosticsWorkerCheckPayload

WORKER_DISPATCH_BUDGET = 15.0
_POLL_INTERVAL_S = 0.25
# Plain failure_category labels mirroring checks.py (no ErrorCategory import in this layer).
_TRANSPORT_FAILURE = "transport_failure"
_INFRASTRUCTURE_FAILURE = "infrastructure_failure"
_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED}
_log = logging.getLogger(__name__)

EnqueueFn = Callable[[str, DiagnosticsWorkerCheckPayload, Authorizing], Awaitable[Job]]
GetFn = Callable[[str], Awaitable[Job | None]]


class WorkerCheckDispatcher(Protocol):
    """Runs the worker-vantage checks and returns their three-state results (or substitutions)."""

    async def run_worker_checks(self) -> list[CheckResult]: ...


def _unavailable(
    detail: str, category: str, *, provider: str, check_ids: Sequence[str]
) -> list[CheckResult]:
    return [
        CheckResult(
            check_id=cid,
            status=CheckStatus.ERROR,
            detail=detail,
            provider=provider,
            failure_category=category,
        )
        for cid in check_ids
    ]


class JobWorkerCheckDispatcher:
    """Enqueues the diagnostics job and bounded-waits for its inline result."""

    def __init__(
        self,
        pool: AsyncConnectionPool | None,
        *,
        provider: str,
        worker_check_ids: Sequence[str],
        budget: float = WORKER_DISPATCH_BUDGET,
        poll_interval: float = _POLL_INTERVAL_S,
        enqueue_fn: EnqueueFn | None = None,
        get_fn: GetFn | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        dedup_suffix: str | None = None,
    ) -> None:
        self._pool = pool
        self._provider = provider
        self._worker_check_ids = tuple(worker_check_ids)
        self._budget = budget
        self._poll_interval = poll_interval
        self._enqueue = enqueue_fn or self._pool_enqueue
        self._get = get_fn or self._pool_get
        self._clock = clock
        self._sleep = sleep_fn
        self._dedup_suffix = dedup_suffix

    def _require_pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("JobWorkerCheckDispatcher needs a pool or an injected seam")
        return self._pool

    async def _pool_enqueue(
        self, dedup_key: str, payload: DiagnosticsWorkerCheckPayload, authorizing: Authorizing
    ) -> Job:
        async with self._require_pool().connection() as conn:
            return await job_queue.enqueue(
                conn,
                JobKind.DIAGNOSTICS_WORKER_CHECK,
                payload,
                authorizing,
                dedup_key,
                max_attempts=1,
            )

    async def _pool_get(self, dedup_key: str) -> Job | None:
        async with self._require_pool().connection() as conn:
            return await job_queue.get_by_dedup_key(conn, dedup_key)

    def _dedup_key(self) -> str:
        return f"diagnostics:{self._provider}:{self._dedup_suffix or uuid.uuid4()}"

    async def run_worker_checks(self) -> list[CheckResult]:
        dedup_key = self._dedup_key()
        # Platform-internal job: it is a read-only side effect of an already-audited operator
        # `ops.diagnostics` call (ADR-0091 §4), not an agent/tenant request, so it carries a
        # synthetic `diagnostics` principal rather than threading the per-request operator identity
        # into this registration-time-built dispatcher. The provider id doubles as the (non-tenant)
        # project so the row is not scoped to any real project's `recent_jobs` view.
        job = await self._enqueue(
            dedup_key,
            DiagnosticsWorkerCheckPayload(provider=self._provider),
            Authorizing(principal="diagnostics", project=self._provider),
        )
        _log.info("diagnostics worker-check job %s enqueued (dedup_key=%s)", job.id, dedup_key)
        start = self._clock()
        while True:
            current = await self._get(dedup_key)
            if current is not None and current.state in _TERMINAL:
                return self._from_terminal(current)
            if self._clock() - start >= self._budget:
                _log.warning("diagnostics job %s not picked up within %ss", dedup_key, self._budget)
                return _unavailable(
                    WORKER_UNAVAILABLE_DETAIL,
                    _TRANSPORT_FAILURE,
                    provider=self._provider,
                    check_ids=self._worker_check_ids,
                )
            await self._sleep(self._poll_interval)

    def _from_terminal(self, job: Job) -> list[CheckResult]:
        if job.state is JobState.SUCCEEDED:
            try:
                results = deserialize_results(job.result_ref)
            except ResultCodecError as exc:
                _log.error("diagnostics job %s returned a malformed result: %s", job.id, exc)
                return _unavailable(
                    "diagnostics worker returned a malformed result",
                    _INFRASTRUCTURE_FAILURE,
                    provider=self._provider,
                    check_ids=self._worker_check_ids,
                )
            _log.info("diagnostics job %s succeeded", job.id)
            return results
        category = job.error_category.value if job.error_category else _INFRASTRUCTURE_FAILURE
        _log.warning("diagnostics job %s ended %s (%s)", job.id, job.state.value, category)
        return _unavailable(
            "diagnostics worker job failed",
            category,
            provider=self._provider,
            check_ids=self._worker_check_ids,
        )
