"""Tests for the diagnostics_worker_check job handler (ADR-0164)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    Check,
    CheckResult,
    CheckStatus,
    Vantage,
)
from kdive.diagnostics.result_codec import deserialize_results
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs.handlers.diagnostics import diagnostics_worker_check_handler


class _FakeCheck(Check):
    def __init__(self, result: CheckResult) -> None:
        self._result = result

    @property
    def id(self) -> str:
        return self._result.check_id

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        return self._result


def _job(provider: str = "remote-libvirt") -> Job:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.DIAGNOSTICS_WORKER_CHECK,
        payload={"provider": provider},
        state=JobState.RUNNING,
        max_attempts=1,
        authorizing={"principal": "diagnostics", "agent_session": None, "project": provider},
        dedup_key=f"diagnostics:{provider}:test",
    )


def test_handler_runs_checks_and_serializes_inline() -> None:
    results = [
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        CheckResult(GDBSTUB_ACL_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
    ]

    async def _run() -> str | None:
        return await diagnostics_worker_check_handler(
            conn=None,
            job=_job(),
            worker_check_builders={"remote-libvirt": lambda: [_FakeCheck(r) for r in results]},
        )

    raw = asyncio.run(_run())
    assert {r.check_id for r in deserialize_results(raw)} == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID}


def test_handler_propagates_config_error() -> None:
    def boom() -> list[Check]:
        raise CategorizedError("bad inventory", category=ErrorCategory.CONFIGURATION_ERROR)

    async def _run() -> str | None:
        return await diagnostics_worker_check_handler(
            conn=None,
            job=_job(),
            worker_check_builders={"remote-libvirt": boom},
        )

    with pytest.raises(CategorizedError):
        asyncio.run(_run())


def test_handler_rejects_unregistered_provider() -> None:
    async def _run() -> str | None:
        return await diagnostics_worker_check_handler(
            conn=None,
            job=_job("other-provider"),
            worker_check_builders={"remote-libvirt": lambda: []},
        )

    with pytest.raises(CategorizedError) as caught:
        asyncio.run(_run())

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details == {"provider": "other-provider"}
