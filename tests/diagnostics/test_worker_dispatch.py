"""Tests for the bounded-wait worker-check dispatcher (ADR-0164)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import pytest

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    CheckResult,
    CheckStatus,
)
from kdive.diagnostics.result_codec import serialize_results
from kdive.diagnostics.worker_dispatch import JobWorkerCheckDispatcher
from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.jobs.payloads import Authorizing, DiagnosticsWorkerCheckPayload


class _FakeJob:
    def __init__(
        self,
        state: JobState,
        result_ref: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> None:
        self.id = "job-1"
        self.state = state
        self.result_ref = result_ref
        self.error_category = error_category


class _FakeQueue:
    """Drives a scripted sequence of job states observed via get_by_dedup_key.

    Matches the injected-seam contract: ``enqueue_fn(dedup_key, payload, authorizing) -> Job`` and
    ``get_fn(dedup_key) -> Job | None``.
    """

    def __init__(self, sequence: Iterable[_FakeJob]) -> None:
        self._sequence = list(sequence)
        self._last: _FakeJob = _FakeJob(JobState.QUEUED)
        self.enqueued: tuple[str, object, object] | None = None

    async def enqueue(self, dedup_key: str, payload: object, authorizing: object) -> _FakeJob:
        self.enqueued = (dedup_key, payload, authorizing)
        return _FakeJob(JobState.QUEUED)

    async def get_by_dedup_key(self, dedup_key: str) -> _FakeJob | None:
        if self._sequence:
            self._last = self._sequence.pop(0)
        return self._last


async def _noop_sleep(_seconds: float) -> None:
    return None


def _dispatcher(
    queue: _FakeQueue,
    *,
    clock_ticks: list[float],
    budget: float = 15.0,
    poll_interval: float = 0.25,
    sleep_fn=_noop_sleep,  # noqa: ANN001
) -> JobWorkerCheckDispatcher:
    ticks = iter(clock_ticks)  # increasing values -> the bounded wait terminates deterministically
    return JobWorkerCheckDispatcher(
        pool=None,
        provider="remote-libvirt",
        worker_check_ids=(PROVIDER_TLS_ID, GDBSTUB_ACL_ID),
        budget=budget,
        poll_interval=poll_interval,
        enqueue_fn=queue.enqueue,  # ty: ignore[invalid-argument-type]
        get_fn=queue.get_by_dedup_key,  # ty: ignore[invalid-argument-type]
        clock=lambda: next(ticks),
        sleep_fn=sleep_fn,
        dedup_suffix="fixed",
    )


def test_succeeded_returns_real_results() -> None:
    out = serialize_results(
        [
            CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
            CheckResult(GDBSTUB_ACL_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        ]
    )
    queue = _FakeQueue([_FakeJob(JobState.SUCCEEDED, result_ref=out)])
    results = asyncio.run(_dispatcher(queue, clock_ticks=[0.0, 0.1]).run_worker_checks())
    assert {r.status for r in results} == {CheckStatus.PASS}
    assert queue.enqueued is not None
    _, payload, authorizing = queue.enqueued
    assert payload == DiagnosticsWorkerCheckPayload(provider="remote-libvirt")
    assert authorizing == Authorizing(principal="diagnostics", project="remote-libvirt")


def test_failed_maps_to_error_with_category() -> None:
    queue = _FakeQueue(
        [_FakeJob(JobState.FAILED, error_category=ErrorCategory.CONFIGURATION_ERROR)]
    )
    results = asyncio.run(_dispatcher(queue, clock_ticks=[0.0, 0.1]).run_worker_checks())
    assert all(r.status is CheckStatus.ERROR for r in results)
    assert {r.check_id for r in results} == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID}
    assert all(r.failure_category == "configuration_error" for r in results)


def test_malformed_result_maps_to_error() -> None:
    queue = _FakeQueue([_FakeJob(JobState.SUCCEEDED, result_ref="not json")])
    results = asyncio.run(_dispatcher(queue, clock_ticks=[0.0, 0.1]).run_worker_checks())
    assert all(r.status is CheckStatus.ERROR for r in results)


def test_pending_then_budget_exhausted_returns_worker_unavailable() -> None:
    queue = _FakeQueue([_FakeJob(JobState.QUEUED)])
    # start clock=0.0; after one pending read, clock=100.0 exceeds budget -> WORKER_UNAVAILABLE
    results = asyncio.run(_dispatcher(queue, clock_ticks=[0.0, 100.0]).run_worker_checks())
    assert all(r.status is CheckStatus.ERROR for r in results)
    assert all("livez" in r.detail for r in results)
    # The synthetic unavailable results carry the dispatcher's provider so the operator
    # view attributes the failure to the right provider, not a null one.
    assert all(r.provider == "remote-libvirt" for r in results)


def test_pending_sleep_is_clamped_to_remaining_budget() -> None:
    queue = _FakeQueue([_FakeJob(JobState.QUEUED), _FakeJob(JobState.QUEUED)])
    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    results = asyncio.run(
        _dispatcher(
            queue,
            clock_ticks=[0.0, 14.9, 15.0],
            budget=15.0,
            poll_interval=0.25,
            sleep_fn=_record_sleep,
        ).run_worker_checks()
    )

    assert sleeps == [pytest.approx(0.1)]
    assert all(r.status is CheckStatus.ERROR for r in results)
