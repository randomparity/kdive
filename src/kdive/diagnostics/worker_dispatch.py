"""Server-side bounded-wait dispatcher for the worker-vantage diagnostic checks (ADR-0163).

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
from collections.abc import Awaitable, Callable
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    CheckResult,
    CheckStatus,
)
from kdive.diagnostics.result_codec import ResultCodecError, deserialize_results
from kdive.diagnostics.service import WORKER_UNAVAILABLE_DETAIL
from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue as job_queue
from kdive.jobs.payloads import Authorizing, DiagnosticsWorkerCheckPayload

_REMOTE_PROVIDER = "remote-libvirt"
WORKER_DISPATCH_BUDGET = 15.0
_POLL_INTERVAL_S = 0.25
# Plain failure_category labels mirroring checks.py (no ErrorCategory import in this layer).
_TRANSPORT_FAILURE = "transport_failure"
_INFRASTRUCTURE_FAILURE = "infrastructure_failure"
_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED}
_WORKER_CHECK_IDS = (PROVIDER_TLS_ID, GDBSTUB_ACL_ID)
_log = logging.getLogger(__name__)

EnqueueFn = Callable[[str, DiagnosticsWorkerCheckPayload, Authorizing], Awaitable[Job]]
GetFn = Callable[[str], Awaitable[Job | None]]


class WorkerCheckDispatcher(Protocol):
    """Runs the worker-vantage checks and returns their three-state results (or substitutions)."""

    async def run_worker_checks(self) -> list[CheckResult]: ...


def _unavailable(detail: str, category: str) -> list[CheckResult]:
    return [
        CheckResult(
            check_id=cid,
            status=CheckStatus.ERROR,
            detail=detail,
            provider=_REMOTE_PROVIDER,
            failure_category=category,
        )
        for cid in _WORKER_CHECK_IDS
    ]


class JobWorkerCheckDispatcher:
    """Enqueues the diagnostics job and bounded-waits for its inline result."""

    def __init__(
        self,
        pool: AsyncConnectionPool | None,
        *,
        budget: float = WORKER_DISPATCH_BUDGET,
        poll_interval: float = _POLL_INTERVAL_S,
        enqueue_fn: EnqueueFn | None = None,
        get_fn: GetFn | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        dedup_suffix: str | None = None,
    ) -> None:
        self._pool = pool
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
        return f"diagnostics:{_REMOTE_PROVIDER}:{self._dedup_suffix or uuid.uuid4()}"

    async def run_worker_checks(self) -> list[CheckResult]:
        dedup_key = self._dedup_key()
        job = await self._enqueue(
            dedup_key,
            DiagnosticsWorkerCheckPayload(provider=_REMOTE_PROVIDER),
            Authorizing(principal="diagnostics", project=_REMOTE_PROVIDER),
        )
        _log.info("diagnostics worker-check job %s enqueued (dedup_key=%s)", job.id, dedup_key)
        start = self._clock()
        while True:
            current = await self._get(dedup_key)
            if current is not None and current.state in _TERMINAL:
                return self._from_terminal(current)
            if self._clock() - start >= self._budget:
                _log.warning("diagnostics job %s not picked up within %ss", dedup_key, self._budget)
                return _unavailable(WORKER_UNAVAILABLE_DETAIL, _TRANSPORT_FAILURE)
            await self._sleep(self._poll_interval)

    def _from_terminal(self, job: Job) -> list[CheckResult]:
        if job.state is JobState.SUCCEEDED:
            try:
                results = deserialize_results(job.result_ref)
            except ResultCodecError as exc:
                _log.error("diagnostics job %s returned a malformed result: %s", job.id, exc)
                return _unavailable(
                    "diagnostics worker returned a malformed result", _INFRASTRUCTURE_FAILURE
                )
            _log.info("diagnostics job %s succeeded", job.id)
            return results
        category = job.error_category.value if job.error_category else _INFRASTRUCTURE_FAILURE
        _log.warning("diagnostics job %s ended %s (%s)", job.id, job.state.value, category)
        return _unavailable("diagnostics worker job failed", category)
