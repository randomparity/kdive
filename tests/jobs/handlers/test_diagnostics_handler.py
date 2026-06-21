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
from kdive.domain.capacity.state import JobState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
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
    assert str(caught.value) == "no diagnostics worker checks are registered for provider"
    assert caught.value.details == {"provider": "other-provider"}


def test_handler_uses_injected_builders_over_the_real_registry() -> None:
    # The handler runs the *injected* builders when they are supplied (the ``or`` default only
    # falls back to the real registry when none are given). A synthetic provider absent from the
    # real registry must still resolve through the injected map.
    result = CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="synthetic-provider")

    async def _run() -> str | None:
        return await diagnostics_worker_check_handler(
            conn=None,
            job=_job("synthetic-provider"),
            worker_check_builders={"synthetic-provider": lambda: [_FakeCheck(result)]},
        )

    raw = asyncio.run(_run())
    assert {r.check_id for r in deserialize_results(raw)} == {PROVIDER_TLS_ID}


class _SlowCheck(Check):
    def __init__(self, check_id: str, delay_s: float) -> None:
        self._check_id = check_id
        self._delay_s = delay_s

    @property
    def id(self) -> str:
        return self._check_id

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        await asyncio.sleep(self._delay_s)
        return CheckResult(self._check_id, CheckStatus.PASS, "ok", provider="remote-libvirt")


def test_handler_bounds_each_check_by_the_per_check_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each check is run under the per-check timeout: a check slower than the bound is reported
    # ERROR ("did not respond"), not its eventual PASS. An unbounded run would let it pass.
    import kdive.jobs.handlers.diagnostics as diagnostics_module

    monkeypatch.setattr(diagnostics_module, "_PER_CHECK_TIMEOUT_S", 0.01)

    async def _run() -> str | None:
        return await diagnostics_worker_check_handler(
            conn=None,
            job=_job(),
            worker_check_builders={
                "remote-libvirt": lambda: [_SlowCheck(PROVIDER_TLS_ID, delay_s=0.2)]
            },
        )

    raw = asyncio.run(_run())
    results = deserialize_results(raw)
    assert [r.status for r in results] == [CheckStatus.ERROR]
    assert "did not respond" in results[0].detail
