"""ADR-0808: a definite authority rejection ends the lifecycle wait; an ambiguous one is bounded."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.services.remote_module_authority_preparation import await_authority_dispatch

_LOGGER = "kdive.services.remote_module_authority_preparation"


def _refused(reason: str = "superseded") -> CategorizedError:
    return CategorizedError(
        f"authority: {reason}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"admission": "refused"},
    )


def _journal_conflict() -> CategorizedError:
    return CategorizedError(
        "authority: journal-conflict",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"admission": "unknown"},
    )


def _transport() -> CategorizedError:
    return CategorizedError(
        "authority: transport-failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE
    )


class _Harness:
    """A scripted dispatch, a fake clock and a sleep that advances it."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.now = 0.0
        self.sleeps: list[float] = []

    async def dispatch(self, _deadline: float) -> str:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else TimeoutError()
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, str)
        return outcome

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def run(self, *, deadline: float = 600.0, jitter: float = 0.0) -> Awaitable[str]:
        return await_authority_dispatch(
            self.dispatch,
            deadline=deadline,
            what="remote module reap",
            clock=lambda: self.now,
            sleep=self.sleep,
            jitter=lambda: jitter,
        )


def _run[T](awaitable: Awaitable[T]) -> T:
    async def main() -> T:
        return await awaitable

    return asyncio.run(main())


def test_a_first_success_returns_without_waiting() -> None:
    harness = _Harness(["done"])
    assert _run(harness.run()) == "done"
    assert (harness.calls, harness.sleeps) == (1, [])


def test_a_definite_rejection_on_first_sight_ends_the_wait_as_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _Harness([_refused()])
    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER),
        pytest.raises(CategorizedError) as raised,
    ):
        _run(harness.run())
    error = raised.value
    assert error.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert error.details["completion"] == "refused-before-mutation"
    assert (harness.calls, harness.sleeps) == (1, [])
    assert any("superseded" in record.getMessage() for record in caplog.records)


def test_a_definite_rejection_after_an_ambiguity_stops_as_unknown_not_refused() -> None:
    harness = _Harness([TimeoutError(), _refused()])
    with pytest.raises(CategorizedError) as raised:
        _run(harness.run())
    assert raised.value.details["completion"] == "unknown"
    assert raised.value.details["admission"] == "refused"
    assert harness.calls == 2


def test_a_journal_conflict_stops_as_unknown_even_on_first_sight() -> None:
    harness = _Harness([_journal_conflict()])
    with pytest.raises(CategorizedError) as raised:
        _run(harness.run())
    assert raised.value.details["completion"] == "unknown"
    assert harness.calls == 1


def test_an_ambiguous_dispatch_backs_off_and_ends_at_the_deadline() -> None:
    harness = _Harness([])
    with pytest.raises(CategorizedError) as raised:
        _run(harness.run(deadline=60.0))
    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert raised.value.details["completion"] == "unknown"
    assert harness.sleeps[0] == pytest.approx(0.1)
    assert harness.sleeps[:-1] == sorted(harness.sleeps[:-1])
    assert max(harness.sleeps) == pytest.approx(2.0)
    assert harness.now == pytest.approx(60.0)
    assert harness.calls < 60


def test_an_ambiguity_that_clears_returns_the_result() -> None:
    harness = _Harness([TimeoutError(), _transport(), "done"])
    assert _run(harness.run()) == "done"
    assert harness.calls == 3


def test_a_failed_after_mutation_completion_stays_terminal() -> None:
    failed = CategorizedError(
        "authority: remote-module-failed",
        category=ErrorCategory.CONFLICT,
        details={"completion": "failed-after-mutation"},
    )
    harness = _Harness([TimeoutError(), failed])
    with pytest.raises(CategorizedError) as raised:
        _run(harness.run())
    assert raised.value is failed


def test_a_non_infrastructure_error_on_first_sight_is_raised() -> None:
    config = CategorizedError("authority: bad", category=ErrorCategory.CONFIGURATION_ERROR)
    harness = _Harness([config])
    with pytest.raises(CategorizedError) as raised:
        _run(harness.run())
    assert raised.value is config
    assert harness.calls == 1


def test_an_unexpected_error_is_raised_first_and_logged_once_ambiguous(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _Harness([RuntimeError("decode")])
    with pytest.raises(RuntimeError):
        _run(first.run())
    later = _Harness([TimeoutError(), RuntimeError("decode"), "done"])
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert _run(later.run()) == "done"
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)


def test_a_cancelled_attempt_is_ambiguous_and_retried() -> None:
    harness = _Harness([asyncio.CancelledError(), "done"])
    assert _run(harness.run()) == "done"
    assert harness.calls == 2


def test_waiting_logs_are_throttled(caplog: pytest.LogCaptureFixture) -> None:
    harness = _Harness([])
    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(CategorizedError):
        _run(harness.run(deadline=600.0))
    assert 15 <= len([r for r in caplog.records if r.name == _LOGGER]) <= 22


def test_the_backoff_is_jittered() -> None:
    harness = _Harness([TimeoutError(), "done"])
    assert _run(harness.run(jitter=1.0)) == "done"
    assert harness.sleeps == [pytest.approx(0.1 * 1.25)]


def test_a_cancellation_during_the_backoff_does_not_abandon_the_wait() -> None:
    harness = _Harness([TimeoutError(), "done"])
    cancelled = False

    async def sleep(seconds: float) -> None:
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            raise asyncio.CancelledError
        await harness.sleep(seconds)

    async def main() -> str:
        return await await_authority_dispatch(
            harness.dispatch,
            deadline=600.0,
            what="remote module reap",
            clock=lambda: harness.now,
            sleep=sleep,
            jitter=lambda: 0.0,
        )

    assert asyncio.run(main()) == "done"
    assert cancelled and harness.calls == 2
