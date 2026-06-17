"""`DiagnosticsService` aggregation tests (ADR-0091 §2, §1 core-up boundary).

The service runs the assembled checks (each bounded by the per-check timeout) and
aggregates them into one report. A down dependency (a worker that cannot pick up the
worker-vantage job) surfaces as an `error` pointing at the health endpoints — **not** a
contract `fail`, and never a hang.
"""

from __future__ import annotations

import asyncio

from kdive.diagnostics.checks import Check, CheckResult, CheckStatus, Vantage
from kdive.diagnostics.service import (
    FEATURE_NOT_ENABLED_DETAIL,
    WORKER_UNAVAILABLE_DETAIL,
    DiagnosticsService,
    WorkerVantageCheck,
    WorkerVantageSubstitution,
    worker_unavailable_results,
)


class _Fixed(Check):
    def __init__(self, result: CheckResult, vantage: Vantage = Vantage.SERVER) -> None:
        self._result = result
        self._vantage = vantage

    @property
    def id(self) -> str:
        return self._result.check_id

    @property
    def vantage(self) -> Vantage:
        return self._vantage

    async def run(self) -> CheckResult:
        return self._result


def _ok(check_id: str) -> CheckResult:
    return CheckResult(check_id=check_id, status=CheckStatus.PASS, detail="ok")


def test_service_runs_every_check_and_collects_results() -> None:
    service = DiagnosticsService(checks=[_Fixed(_ok("a")), _Fixed(_ok("b"))], per_check_timeout=1.0)
    report = asyncio.run(service.run())
    assert {r.check_id for r in report.results} == {"a", "b"}
    assert all(r.status is CheckStatus.PASS for r in report.results)


def test_service_has_failed_when_any_check_fails() -> None:
    fail = CheckResult(check_id="c", status=CheckStatus.FAIL, detail="broke", fix="do it")
    service = DiagnosticsService(checks=[_Fixed(_ok("a")), _Fixed(fail)], per_check_timeout=1.0)
    report = asyncio.run(service.run())
    assert report.has_failure is True
    assert report.has_error is False


def test_service_error_does_not_count_as_failure() -> None:
    err = CheckResult(check_id="c", status=CheckStatus.ERROR, detail="provider down")
    service = DiagnosticsService(checks=[_Fixed(_ok("a")), _Fixed(err)], per_check_timeout=1.0)
    report = asyncio.run(service.run())
    assert report.has_failure is False
    assert report.has_error is True


class _Slow(_Fixed):
    def __init__(self, result: CheckResult, *, delay: float) -> None:
        super().__init__(result)
        self._delay = delay

    async def run(self) -> CheckResult:
        await asyncio.sleep(self._delay)
        return self._result


def test_overall_deadline_reports_unrun_checks_as_error() -> None:
    service = DiagnosticsService(
        checks=[_Slow(_ok("a"), delay=0.05), _Fixed(_ok("b"))],
        per_check_timeout=1.0,
        overall_timeout=0.01,
    )
    report = asyncio.run(service.run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id["b"].status is CheckStatus.ERROR
    assert by_id["b"].fix is None
    assert "deadline" in by_id["b"].detail
    assert report.has_failure is False


def test_overall_deadline_unset_runs_every_check() -> None:
    service = DiagnosticsService(
        checks=[_Fixed(_ok("a")), _Fixed(_ok("b"))],
        per_check_timeout=1.0,
        overall_timeout=None,
    )
    report = asyncio.run(service.run())
    assert all(r.status is CheckStatus.PASS for r in report.results)


def test_worker_unavailable_yields_error_pointing_at_health() -> None:
    worker_checks = [
        _Fixed(_ok("provider_tls"), Vantage.WORKER),
        _Fixed(_ok("gdbstub_acl"), Vantage.WORKER),
    ]
    results = worker_unavailable_results(worker_checks)
    assert [r.status for r in results] == [CheckStatus.ERROR, CheckStatus.ERROR]
    assert all(r.fix is None for r in results)
    assert all(WORKER_UNAVAILABLE_DETAIL in r.detail for r in results)
    assert all(r.failure_category == "transport_failure" for r in results)


def test_worker_unavailable_results_default_reason_is_worker_down() -> None:
    # A bare call (no reason) keeps the historical /livez-/readyz worker-down meaning.
    results = worker_unavailable_results([_Fixed(_ok("provider_tls"), Vantage.WORKER)])
    assert WORKER_UNAVAILABLE_DETAIL in results[0].detail
    assert FEATURE_NOT_ENABLED_DETAIL not in results[0].detail


def test_feature_not_enabled_substitution_does_not_point_at_health() -> None:
    results = worker_unavailable_results(
        [_Fixed(_ok("provider_tls"), Vantage.WORKER)],
        reason=WorkerVantageSubstitution.FEATURE_NOT_ENABLED,
    )
    assert results[0].status is CheckStatus.ERROR
    assert results[0].fix is None
    assert FEATURE_NOT_ENABLED_DETAIL in results[0].detail
    assert "/livez" not in results[0].detail
    assert "/readyz" not in results[0].detail
    assert results[0].failure_category == "not_implemented"


def test_feature_not_enabled_and_worker_down_are_category_distinguishable() -> None:
    enabled = worker_unavailable_results(
        [_Fixed(_ok("a"), Vantage.WORKER)],
        reason=WorkerVantageSubstitution.FEATURE_NOT_ENABLED,
    )[0]
    down = worker_unavailable_results(
        [_Fixed(_ok("a"), Vantage.WORKER)],
        reason=WorkerVantageSubstitution.WORKER_UNAVAILABLE,
    )[0]
    assert enabled.failure_category != down.failure_category


def test_unavailable_worker_metadata_yields_error_without_runnable_check() -> None:
    service = DiagnosticsService(
        checks=[],
        per_check_timeout=1.0,
        substitution_reason=WorkerVantageSubstitution.FEATURE_NOT_ENABLED,
        unavailable_worker_checks=[
            WorkerVantageCheck(id="provider_tls", provider="remote-libvirt")
        ],
    )
    report = asyncio.run(service.run())
    result = report.results[0]
    assert result.check_id == "provider_tls"
    assert result.provider == "remote-libvirt"
    assert result.status is CheckStatus.ERROR
    assert FEATURE_NOT_ENABLED_DETAIL in result.detail
    assert result.failure_category == "not_implemented"


def test_service_substitutes_worker_results_when_worker_down() -> None:
    service = DiagnosticsService(
        checks=[_Fixed(_ok("a"), Vantage.WORKER)],
        per_check_timeout=1.0,
        worker_available=False,
    )
    report = asyncio.run(service.run())
    assert report.results[0].status is CheckStatus.ERROR
    assert report.has_error is True
    assert WORKER_UNAVAILABLE_DETAIL in report.results[0].detail


def test_service_substitution_reason_threads_into_results() -> None:
    service = DiagnosticsService(
        checks=[_Fixed(_ok("a"), Vantage.WORKER)],
        per_check_timeout=1.0,
        worker_available=False,
        substitution_reason=WorkerVantageSubstitution.FEATURE_NOT_ENABLED,
    )
    report = asyncio.run(service.run())
    assert report.results[0].status is CheckStatus.ERROR
    assert FEATURE_NOT_ENABLED_DETAIL in report.results[0].detail
    assert report.results[0].failure_category == "not_implemented"
