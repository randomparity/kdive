"""Worker handler for capture_traffic: poll loop + snapshot/store (ADR-0385)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.jobs.capture_operations.supervisor import (
    CaptureAuthorityLost,
    CaptureSnapshot,
    capture_authority_scope,
)
from kdive.jobs.handlers.control import capture_traffic
from kdive.jobs.handlers.control.capture_traffic import (
    POLL_INTERVAL_SECONDS,
    LoopResult,
    run_capture_loop,
)


def _run(sizes, *, canceled_at=None, max_bytes=10_000, max_polls=5):
    sleeps: list[float] = []

    async def stat():
        i = min(len(sleeps), len(sizes) - 1)
        return sizes[i]

    async def sleep(seconds):
        sleeps.append(seconds)

    async def canceled():
        return canceled_at is not None and len(sleeps) >= canceled_at

    result = asyncio.run(
        run_capture_loop(
            stat=stat, sleep=sleep, canceled=canceled, max_bytes=max_bytes, max_polls=max_polls
        )
    )
    return result, {"sleeps": sleeps}


def test_loop_stops_at_duration() -> None:
    result, calls = _run([100, 200, 300], max_polls=3)
    assert result == LoopResult(truncated=False, canceled=False)
    # Every poll sleeps for exactly the poll interval before re-checking size/cancel.
    assert calls["sleeps"] == [POLL_INTERVAL_SECONDS] * 3


def test_loop_stops_at_max_bytes() -> None:
    result, _ = _run([100, 5000, 20000], max_bytes=10_000, max_polls=9)
    assert result.truncated is True
    assert result.canceled is False


def test_loop_truncates_at_exact_max_bytes() -> None:
    # The size guard is ``>=``: a file that reaches max_bytes exactly counts as truncated.
    result, _ = _run([10_000], max_bytes=10_000, max_polls=9)
    assert result.truncated is True
    assert result.canceled is False


def test_loop_stops_on_cancel() -> None:
    result, _ = _run([100, 100, 100], canceled_at=2, max_polls=9)
    assert result.canceled is True
    assert result.truncated is False


def test_handler_delegates_provider_phase_to_supervisor_without_publication_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run_handler() -> None:
        run_id = uuid4()
        job = Job(
            id=uuid4(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            kind=JobKind.CAPTURE_TRAFFIC,
            payload={
                "run_id": str(run_id),
                "duration_s": 1,
                "max_bytes": 1_048_576,
                "snaplen": 128,
            },
            state=JobState.RUNNING,
            attempt=1,
            max_attempts=3,
            worker_id="worker-a",
            authorizing={"principal": "p", "agent_session": None, "project": "proj"},
            dedup_key="capture-supervised",
        )
        snapshot = CaptureSnapshot(
            provider_kind="local-libvirt",
            resource_id=uuid4(),
            system_id=uuid4(),
            domain_name="guest",
            project="proj",
            write_remediation="fix permissions",
            configuration=lambda: b"config",
            quiescence=cast(Any, lambda _config: SimpleNamespace()),
        )
        observed: list[object] = []

        async def snapshot_run(*args: object) -> CaptureSnapshot:
            return snapshot

        class _Supervisor:
            async def execute(
                self,
                conn: object,
                supplied_job: Job,
                supplied_snapshot: CaptureSnapshot,
                request: CaptureRequest,
            ) -> bytes | None:
                observed.extend((conn, supplied_job, supplied_snapshot, request))
                return None

        monkeypatch.setattr(capture_traffic, "_snapshot", snapshot_run)
        result = await capture_traffic.capture_traffic_handler(
            cast(Any, SimpleNamespace()),
            job,
            resolver=cast(Any, SimpleNamespace()),
            artifact_store=cast(Any, SimpleNamespace()),
            supervisor=cast(Any, _Supervisor()),
        )
        assert result is None
        request = cast(CaptureRequest, observed[3])
        assert request.job_id == job.id
        assert request.max_polls == 2
        assert request.resource_id == snapshot.resource_id

    asyncio.run(_run_handler())


def test_handler_does_not_publish_when_worker_authority_ends_with_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run_handler() -> list[str]:
        authority_lost = asyncio.Event()
        events: list[str] = []
        run_id = uuid4()
        job = Job(
            id=uuid4(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            kind=JobKind.CAPTURE_TRAFFIC,
            payload={
                "run_id": str(run_id),
                "duration_s": 1,
                "max_bytes": 1_048_576,
                "snaplen": 128,
            },
            state=JobState.RUNNING,
            attempt=1,
            max_attempts=3,
            worker_id="worker-a",
            authorizing={"principal": "p", "agent_session": None, "project": "proj"},
            dedup_key="capture-authority-publication",
        )
        snapshot = CaptureSnapshot(
            provider_kind="local-libvirt",
            resource_id=uuid4(),
            system_id=uuid4(),
            domain_name="guest",
            project="proj",
            write_remediation="fix permissions",
            configuration=lambda: b"config",
            quiescence=cast(Any, lambda _config: SimpleNamespace()),
        )

        async def snapshot_run(*args: object) -> CaptureSnapshot:
            return snapshot

        class _Supervisor:
            async def execute(self, *args: object) -> bytes:
                authority_lost.set()
                return b"x" * 24

        async def store(*args: object) -> object:
            events.append("published")
            return uuid4()

        monkeypatch.setattr(capture_traffic, "_snapshot", snapshot_run)
        monkeypatch.setattr(capture_traffic, "_store_capture", store)
        with capture_authority_scope(authority_lost), pytest.raises(CaptureAuthorityLost):
            await capture_traffic.capture_traffic_handler(
                cast(Any, SimpleNamespace()),
                job,
                resolver=cast(Any, SimpleNamespace()),
                artifact_store=cast(Any, SimpleNamespace()),
                supervisor=cast(Any, _Supervisor()),
            )
        return events

    assert asyncio.run(_run_handler()) == []
