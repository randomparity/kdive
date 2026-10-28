"""The remote running-kernel observation waits for the guest agent, within its deadline (#3376).

A freshly started guest has no connected agent for a short while. The one-shot identity read
reports that as a retryable ``TRANSPORT_FAILURE``; the observation is the wait, bounded by the
call's own deadline, and nothing but that category is retried.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt import external_boot_operations as operations_module
from kdive.providers.remote_libvirt.external_boot_operations import (
    ConcreteRemoteExternalBootOperations,
)

_SYSTEM = "00000000-0000-4000-8000-000000000001"
_OBSERVED = object()


class _Clock:
    """A monotonic clock the injected sleep advances, so no test waits for real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Domain:
    def name(self) -> str:
        return f"kdive-{_SYSTEM}"


class _Connection:
    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def lookupByName(self, name: str) -> _Domain:  # noqa: N802 - libvirt naming
        assert name == f"kdive-{_SYSTEM}"
        return _Domain()


def _operations(clock: _Clock) -> ConcreteRemoteExternalBootOperations:
    return ConcreteRemoteExternalBootOperations(
        cast(Any, object()),
        cast(Any, _Connection),
        "modules",
        clock.monotonic,
        cast(Any, object()),
        sleep=clock.sleep,
    )


def _observe(clock: _Clock, deadline: float) -> object:
    recovery = SimpleNamespace(binding=SimpleNamespace(system_id=_SYSTEM), definition=object())
    return _operations(clock).observe(
        cast(Any, recovery), OpaqueProviderRef(ref="authority/current"), deadline
    )


@pytest.fixture(autouse=True)
def _owned_artifacts_are_not_under_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ConcreteRemoteExternalBootOperations, "_validate_owned_artifacts", lambda *_: None
    )


def _identity_reads(monkeypatch: pytest.MonkeyPatch, failures: list[CategorizedError]) -> list[int]:
    calls: list[int] = []

    def read(*_args: object) -> object:
        calls.append(len(calls))
        if failures:
            raise failures.pop(0)
        return _OBSERVED

    monkeypatch.setattr(operations_module, "observe_guest_identity", read)
    return calls


def _failure(category: ErrorCategory) -> CategorizedError:
    return CategorizedError("agent", category=category)


def test_an_agent_that_connects_after_some_failures_is_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    calls = _identity_reads(monkeypatch, [_failure(ErrorCategory.TRANSPORT_FAILURE)] * 3)

    assert _observe(clock, deadline=300.0) is _OBSERVED
    assert len(calls) == 4
    assert clock.sleeps == [1.0, 1.0, 1.0]


def test_a_permanent_category_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    calls = _identity_reads(monkeypatch, [_failure(ErrorCategory.CONFIGURATION_ERROR)])

    with pytest.raises(CategorizedError) as refused:
        _observe(clock, deadline=300.0)

    assert refused.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert len(calls) == 1
    assert clock.sleeps == []


def test_an_agent_that_never_connects_fails_as_a_readiness_timeout_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    calls = _identity_reads(monkeypatch, [_failure(ErrorCategory.TRANSPORT_FAILURE)] * 1000)

    with pytest.raises(CategorizedError) as expired:
        _observe(clock, deadline=10.0)

    assert expired.value.category is ErrorCategory.READINESS_FAILURE
    assert isinstance(expired.value.__cause__, CategorizedError)
    assert expired.value.__cause__.category is ErrorCategory.TRANSPORT_FAILURE
    assert clock.now == pytest.approx(10.0)
    assert len(calls) > 1


def test_the_wait_never_sleeps_past_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _identity_reads(monkeypatch, [_failure(ErrorCategory.TRANSPORT_FAILURE)] * 1000)

    with pytest.raises(CategorizedError):
        _observe(clock, deadline=0.7)

    assert clock.now <= 0.7 + 1e-9
    assert all(step > 0 for step in clock.sleeps)


def test_an_expired_deadline_is_refused_before_any_read(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    clock.now = 5.0
    calls = _identity_reads(monkeypatch, [])

    with pytest.raises(TimeoutError, match="observation deadline"):
        _observe(clock, deadline=5.0)

    assert calls == []


def test_the_wait_is_capped_below_a_distant_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _identity_reads(monkeypatch, [_failure(ErrorCategory.TRANSPORT_FAILURE)] * 10_000)

    with pytest.raises(CategorizedError) as expired:
        _observe(clock, deadline=3600.0)

    assert expired.value.category is ErrorCategory.READINESS_FAILURE
    assert clock.now == pytest.approx(90.0)
