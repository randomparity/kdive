"""The one router that diverts an authority-marked job (ADR-0593 decision 3).

The router branches on **presence** of the marker key, not on validity of the marker — the same
rule ``src/kdive/jobs/worker.py:614-624`` already applies, whose comment states "Presence, rather
than validity, selects the fail-closed path". A malformed marker must therefore fail inside the
operations registry rather than boot a Run or tear a System down.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest
from psycopg import AsyncConnection

from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.external_boot.operations import ExternalBootOperations
from kdive.jobs.handlers.external_boot.router import route_marked
from kdive.jobs.models import JobHandlerResult
from kdive.jobs.payloads import EXTERNAL_BOOT_AUTHORITY_MARKER_KEY
from tests.jobs.handlers.external_boot.support import build_job, marker_fields


class _RecordingOperations:
    """Stands in for ``ExternalBootOperations`` and records every dispatch."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def run(self, _conn: AsyncConnection, job: Job) -> Any:
        self.jobs.append(job)
        return None


def _ordinary(calls: list[Job]) -> Callable[[AsyncConnection, Job], Any]:
    async def handler(_conn: AsyncConnection, job: Job) -> JobHandlerResult:
        calls.append(job)
        return "ordinary"

    return handler


def _route(operations: _RecordingOperations, calls: list[Job]) -> Any:
    return route_marked(cast(ExternalBootOperations, operations), _ordinary(calls))


@pytest.mark.parametrize(
    ("kind", "payload_key"), [(JobKind.BOOT, "run_id"), (JobKind.TEARDOWN, "system_id")]
)
def test_unmarked_job_reaches_the_ordinary_handler(kind: JobKind, payload_key: str) -> None:
    operations, ordinary_calls = _RecordingOperations(), []
    job = build_job(kind, {payload_key: str(marker_fields()["run_id"])})

    result = asyncio.run(_route(operations, ordinary_calls)(cast(AsyncConnection, None), job))

    assert result == "ordinary"
    assert ordinary_calls == [job]
    assert operations.jobs == []


@pytest.mark.parametrize("kind", [JobKind.BOOT, JobKind.TEARDOWN])
def test_marked_job_does_not_reach_the_ordinary_handler(kind: JobKind) -> None:
    """Asserts both sides, so the test cannot pass by the job reaching neither handler."""
    operations, ordinary_calls = _RecordingOperations(), []
    fields = marker_fields(
        purpose="teardown" if kind is JobKind.TEARDOWN else "activate",
        operation="teardown" if kind is JobKind.TEARDOWN else "activate",
    )
    key = "system_id" if kind is JobKind.TEARDOWN else "run_id"
    job = build_job(kind, {key: fields[key], EXTERNAL_BOOT_AUTHORITY_MARKER_KEY: fields})

    asyncio.run(_route(operations, ordinary_calls)(cast(AsyncConnection, None), job))

    assert ordinary_calls == []
    assert operations.jobs == [job]


@pytest.mark.parametrize(
    ("kind", "payload_key"), [(JobKind.BOOT, "run_id"), (JobKind.TEARDOWN, "system_id")]
)
def test_malformed_marker_does_not_reach_the_ordinary_handler(
    kind: JobKind, payload_key: str
) -> None:
    """Presence of the key, not validity of its value, selects the fail-closed path.

    A marker the models cannot decode must still be diverted: reaching ``boot_handler`` or
    ``teardown_handler`` would boot a Run or tear a System down under an activation that restricts
    it.
    """
    operations, ordinary_calls = _RecordingOperations(), []
    job = build_job(
        kind,
        {
            payload_key: str(marker_fields()["run_id"]),
            EXTERNAL_BOOT_AUTHORITY_MARKER_KEY: {"nonsense": 1},
        },
    )

    asyncio.run(_route(operations, ordinary_calls)(cast(AsyncConnection, None), job))

    assert ordinary_calls == []
    assert operations.jobs == [job]
