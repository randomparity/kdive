"""Whole-report and local dispatch budgets, including hung queue boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from kdive.diagnostics.checks import CheckResult, CheckStatus
from kdive.diagnostics.result_codec import serialize_results
from kdive.diagnostics.service import (
    DiagnosticsService,
    WorkerVantageDispatchMode,
    _CompositeWorkerCheckDispatcher,
)
from kdive.diagnostics.worker_dispatch import JobWorkerCheckDispatcher
from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.payloads import Authorizing, DiagnosticsWorkerCheckPayload
from tests.diagnostics.checks_support import FixedCheck as _Fixed
from tests.diagnostics.checks_support import SlowCheck as _Slow


class _Queue:
    """Replace queue I/O while exercising actual dispatcher timing and report policy."""

    def __init__(self, provider: str, *, blocked: str | None = None) -> None:
        self.provider = provider
        self.blocked = blocked
        self.enqueues = 0
        self.entered = asyncio.Event()
        now = datetime(2026, 1, 1, tzinfo=UTC)
        self.result = CheckResult(
            f"{provider}.check", CheckStatus.PASS, "checked", provider=provider
        )
        self.job = Job(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            created_at=now,
            updated_at=now,
            kind=JobKind.DIAGNOSTICS_WORKER_CHECK,
            state=JobState.QUEUED,
            max_attempts=1,
            authorizing=Authorizing(principal="diagnostics", project=provider).model_dump(),
            dedup_key=f"diagnostics:{provider}:test",
        )

    async def enqueue(
        self, key: str, payload: DiagnosticsWorkerCheckPayload, authorizing: Authorizing
    ) -> Job:
        self.enqueues += 1
        if self.blocked == "enqueue":
            self.entered.set()
            await asyncio.Event().wait()
        return self.job

    async def get(self, key: str) -> Job:
        if self.blocked == "poll":
            self.entered.set()
            await asyncio.Event().wait()
        if self.blocked == "sleep":
            return self.job
        return self.job.model_copy(
            update={"state": JobState.SUCCEEDED, "result_ref": serialize_results([self.result])}
        )

    async def sleep(self, seconds: float) -> None:
        if self.blocked == "sleep":
            self.entered.set()
            await asyncio.Event().wait()
        await asyncio.sleep(seconds)

    def dispatcher(self, *, budget: float = 0.02) -> JobWorkerCheckDispatcher:
        return JobWorkerCheckDispatcher(
            None,
            provider=self.provider,
            worker_check_ids=(self.result.check_id,),
            budget=budget,
            enqueue_fn=self.enqueue,
            get_fn=self.get,
            sleep_fn=self.sleep,
        )


def _assert_unavailable(results: list[CheckResult], provider: str) -> None:
    assert len(results) == 1
    result = results[0]
    assert result.check_id == f"{provider}.check"
    assert result.provider == provider
    assert result.status is CheckStatus.ERROR
    assert result.failure_category is ErrorCategory.TRANSPORT_FAILURE
    assert result.fix is None
    assert "readyz" in result.detail


@pytest.mark.parametrize("blocked", ["enqueue", "poll", "sleep"])
@pytest.mark.parametrize("overall", [None, 0.04])
def test_hung_queue_awaits_are_bounded(blocked: str, overall: float | None) -> None:
    async def run() -> None:
        queue = _Queue("provider-a", blocked=blocked)
        service = DiagnosticsService(
            checks=[],
            per_check_timeout=1,
            overall_timeout=overall,
            worker_mode=WorkerVantageDispatchMode(queue.dispatcher()),
        )
        async with asyncio.timeout(2):
            report = await service.run()
        assert queue.entered.is_set()
        _assert_unavailable(report.results, queue.provider)

    asyncio.run(run())


@pytest.mark.parametrize("budget", [0.0, -1.0])
def test_exhausted_local_budget_never_enqueues(budget: float) -> None:
    async def run() -> None:
        queue = _Queue("provider-a")
        results = await queue.dispatcher(budget=budget).run_worker_checks()
        assert queue.enqueues == 0
        _assert_unavailable(results, queue.provider)

    asyncio.run(run())


def test_expired_propagated_deadline_never_enqueues() -> None:
    async def run() -> None:
        queue = _Queue("provider-a")
        results = await queue.dispatcher().run_worker_checks(
            deadline=asyncio.get_running_loop().time() - 1
        )
        assert queue.enqueues == 0
        _assert_unavailable(results, queue.provider)

    asyncio.run(run())


def test_propagated_deadline_bounds_a_long_local_budget() -> None:
    async def run() -> None:
        queue = _Queue("provider-a", blocked="enqueue")
        async with asyncio.timeout(2):
            results = await queue.dispatcher(budget=30).run_worker_checks(
                deadline=asyncio.get_running_loop().time() + 0.02
            )
        _assert_unavailable(results, queue.provider)

    asyncio.run(run())


def test_composite_forwards_one_deadline_to_every_provider() -> None:
    async def run() -> None:
        received: list[float | None] = []

        class Recorder:
            async def run_worker_checks(
                self, *, deadline: float | None = None
            ) -> list[CheckResult]:
                received.append(deadline)
                return []

        deadline = asyncio.get_running_loop().time() + 30
        await _CompositeWorkerCheckDispatcher([Recorder(), Recorder()]).run_worker_checks(
            deadline=deadline
        )
        assert received == [deadline, deadline]

    asyncio.run(run())


def test_shared_phase_keeps_completed_results_and_does_not_start_expired_provider() -> None:
    async def run() -> None:
        completed = _Queue("provider-a")
        hung = _Queue("provider-b", blocked="poll")
        unstarted = _Queue("provider-c")
        server = CheckResult("server", CheckStatus.PASS, "server checked")
        service = DiagnosticsService(
            checks=[_Fixed(server)],
            per_check_timeout=1,
            overall_timeout=0.04,
            worker_mode=WorkerVantageDispatchMode(
                _CompositeWorkerCheckDispatcher(
                    [queue.dispatcher(budget=30) for queue in (completed, hung, unstarted)]
                )
            ),
        )
        async with asyncio.timeout(2):
            report = await service.run()
        assert report.results[:2] == [server, completed.result]
        _assert_unavailable(report.results[2:3], hung.provider)
        _assert_unavailable(report.results[3:], unstarted.provider)
        assert [queue.enqueues for queue in (completed, hung, unstarted)] == [1, 1, 0]

    asyncio.run(run())


def test_slow_server_check_leaves_reserved_worker_opportunity() -> None:
    async def run() -> None:
        queue = _Queue("provider-a")
        service = DiagnosticsService(
            checks=[
                _Slow(CheckResult("server", CheckStatus.PASS, "late"), delay=30),
                _Fixed(CheckResult("unstarted-server", CheckStatus.PASS, "not run")),
            ],
            per_check_timeout=30,
            overall_timeout=0.1,
            worker_mode=WorkerVantageDispatchMode(queue.dispatcher(budget=30)),
        )
        async with asyncio.timeout(2):
            report = await service.run()
        assert report.results[0].status is CheckStatus.ERROR
        assert report.results[1].status is CheckStatus.ERROR
        assert "server diagnostics deadline" in report.results[1].detail
        assert "worker time is reserved" in report.results[1].detail
        assert report.results[2:] == [queue.result]

    asyncio.run(run())


@pytest.mark.parametrize("overall", [0.0, -1.0])
def test_exhausted_report_never_dispatches(overall: float) -> None:
    async def run() -> None:
        queue = _Queue("provider-a")
        service = DiagnosticsService(
            checks=[],
            per_check_timeout=1,
            overall_timeout=overall,
            worker_mode=WorkerVantageDispatchMode(queue.dispatcher()),
        )
        report = await service.run()
        assert queue.enqueues == 0
        _assert_unavailable(report.results, queue.provider)

    asyncio.run(run())


@pytest.mark.parametrize("blocked", ["enqueue", "poll", "sleep"])
def test_caller_cancellation_is_not_a_diagnostic_verdict(blocked: str) -> None:
    async def run() -> None:
        queue = _Queue("provider-a", blocked=blocked)
        service = DiagnosticsService(
            checks=[],
            per_check_timeout=1,
            overall_timeout=30,
            worker_mode=WorkerVantageDispatchMode(queue.dispatcher(budget=30)),
        )
        async with asyncio.timeout(2):
            task = asyncio.create_task(service.run())
            try:
                await queue.entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["enqueue", "poll"])
def test_queue_timeout_error_keeps_infrastructure_category(boundary: str) -> None:
    async def run() -> None:
        queue = _Queue("provider-a")

        async def enqueue(
            key: str, payload: DiagnosticsWorkerCheckPayload, authorizing: Authorizing
        ) -> Job:
            raise TimeoutError("database statement timeout")

        async def get(key: str) -> Job:
            raise TimeoutError("database statement timeout")

        dispatcher = JobWorkerCheckDispatcher(
            None,
            provider=queue.provider,
            worker_check_ids=(queue.result.check_id,),
            enqueue_fn=enqueue if boundary == "enqueue" else queue.enqueue,
            get_fn=get if boundary == "poll" else queue.get,
        )
        results = await dispatcher.run_worker_checks()
        assert len(results) == 1
        assert results[0].failure_category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert "database statement timeout" not in results[0].detail

    asyncio.run(run())


def test_non_budget_sleep_timeout_is_not_reclassified() -> None:
    async def run() -> None:
        queue = _Queue("provider-a", blocked="sleep")

        async def sleep(seconds: float) -> None:
            raise TimeoutError("independent sleep failure")

        dispatcher = JobWorkerCheckDispatcher(
            None,
            provider=queue.provider,
            worker_check_ids=(queue.result.check_id,),
            enqueue_fn=queue.enqueue,
            get_fn=queue.get,
            sleep_fn=sleep,
        )
        with pytest.raises(TimeoutError, match="independent sleep failure"):
            await dispatcher.run_worker_checks()

    asyncio.run(run())


def test_injected_elapsed_budget_includes_enqueue_time() -> None:
    async def run() -> None:
        queue = _Queue("provider-a", blocked="sleep")
        elapsed = 0.0
        sleeps: list[float] = []

        async def enqueue(
            key: str, payload: DiagnosticsWorkerCheckPayload, authorizing: Authorizing
        ) -> Job:
            nonlocal elapsed
            job = await queue.enqueue(key, payload, authorizing)
            elapsed += 10
            return job

        async def sleep(seconds: float) -> None:
            nonlocal elapsed
            sleeps.append(seconds)
            elapsed += 10

        dispatcher = JobWorkerCheckDispatcher(
            None,
            provider=queue.provider,
            worker_check_ids=(queue.result.check_id,),
            budget=1,
            enqueue_fn=enqueue,
            get_fn=queue.get,
            clock=lambda: elapsed,
            sleep_fn=sleep,
        )
        _assert_unavailable(await dispatcher.run_worker_checks(), queue.provider)
        assert sleeps == []

    asyncio.run(run())
