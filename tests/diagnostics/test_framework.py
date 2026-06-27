"""Framework tests for the `Check`/three-state `CheckResult` abstraction (ADR-0091 §2).

The three-state distinction is load-bearing: `fail` means a contract is violated and
`fix` is the exact remediation; `error` means the check could not be run to a verdict
(detail says what blocked it, never a contract-fix string); `pass` is a clean read. A
check that does not answer within its per-check timeout is `error`, not a hang.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from kdive.diagnostics.checks import Check, CheckResult, CheckStatus, Vantage, run_check
from kdive.domain.errors import ErrorCategory


class _Static(Check):
    """A check returning a fixed result, for framework-level assertions."""

    def __init__(self, result: CheckResult, *, delay: float = 0.0) -> None:
        self._result = result
        self._delay = delay

    @property
    def id(self) -> str:
        return self._result.check_id

    @property
    def vantage(self) -> Vantage:
        return Vantage.SERVER

    async def run(self) -> CheckResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result


def test_fail_result_requires_a_fix() -> None:
    with pytest.raises(ValueError, match="fix"):
        CheckResult(check_id="x", status=CheckStatus.FAIL, detail="broken", fix=None)


def test_error_result_forbids_a_fix() -> None:
    with pytest.raises(ValueError, match="fix"):
        CheckResult(
            check_id="x", status=CheckStatus.ERROR, detail="provider down", fix="do the thing"
        )


def test_pass_result_forbids_a_fix() -> None:
    with pytest.raises(ValueError, match="fix"):
        CheckResult(check_id="x", status=CheckStatus.PASS, detail="ok", fix="do the thing")


def test_pass_result_is_clean() -> None:
    result = CheckResult(check_id="x", status=CheckStatus.PASS, detail="ok")
    assert result.fix is None
    assert result.provider is None


def test_pass_result_forbids_a_failure_category() -> None:
    with pytest.raises(ValueError, match="failure_category"):
        CheckResult(
            check_id="x",
            status=CheckStatus.PASS,
            detail="ok",
            failure_category=ErrorCategory.TRANSPORT_FAILURE,
        )


def test_fail_result_may_carry_a_failure_category() -> None:
    result = CheckResult(
        check_id="x",
        status=CheckStatus.FAIL,
        detail="host down",
        fix="bring it up",
        failure_category=ErrorCategory.TRANSPORT_FAILURE,
    )
    assert result.failure_category == "transport_failure"


def test_error_result_may_carry_a_failure_category() -> None:
    result = CheckResult(
        check_id="x",
        status=CheckStatus.ERROR,
        detail="bad config",
        failure_category=ErrorCategory.CONFIGURATION_ERROR,
    )
    assert result.failure_category == "configuration_error"


def test_failure_category_defaults_to_none() -> None:
    result = CheckResult(check_id="x", status=CheckStatus.PASS, detail="ok")
    assert result.failure_category is None


def test_data_defaults_to_none() -> None:
    result = CheckResult(check_id="x", status=CheckStatus.PASS, detail="ok")
    assert result.data is None


def test_data_carries_structured_fields_on_any_status() -> None:
    # The structured machine-readable companion to ``detail``; legal on any status,
    # like ``resource_id``.
    result = CheckResult(
        check_id="x",
        status=CheckStatus.PASS,
        detail="ok",
        data={"resolved_path": "/abs/tree", "vantage": "server"},
    )
    assert result.data == {"resolved_path": "/abs/tree", "vantage": "server"}


def test_run_check_returns_the_checks_result() -> None:
    expected = CheckResult(check_id="x", status=CheckStatus.PASS, detail="ok")
    result = asyncio.run(run_check(_Static(expected), timeout=1.0))
    assert result is expected


def test_run_check_maps_timeout_to_error() -> None:
    slow = _Static(CheckResult(check_id="slow", status=CheckStatus.PASS, detail="ok"), delay=10.0)
    result = asyncio.run(run_check(slow, timeout=0.01))
    assert result.status is CheckStatus.ERROR
    assert "did not respond within" in result.detail
    assert result.fix is None
    assert result.check_id == "slow"


def test_run_check_maps_unexpected_exception_to_error(caplog: pytest.LogCaptureFixture) -> None:
    class _Boom(_Static):
        async def run(self) -> CheckResult:
            raise RuntimeError("backend exploded")

    caplog.set_level(logging.ERROR, logger="kdive.diagnostics.checks")
    boom = _Boom(CheckResult(check_id="boom", status=CheckStatus.PASS, detail="ok"))
    result = asyncio.run(run_check(boom, timeout=1.0))
    assert result.status is CheckStatus.ERROR
    assert result.fix is None
    assert result.check_id == "boom"
    assert "backend exploded" not in result.detail
    assert "backend exploded" in caplog.text
    assert any(record.exc_info is not None for record in caplog.records)
