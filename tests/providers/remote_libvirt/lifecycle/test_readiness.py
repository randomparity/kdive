"""Unit tests for the build-VM network-readiness poll loop (ADR-0144)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.readiness import wait_for_network


def _ticker(step: float = 1.0) -> Callable[[], float]:
    now = {"t": 0.0}

    def _monotonic() -> float:
        current = now["t"]
        now["t"] += step
        return current

    return _monotonic


def _sequence_probe(returns: list[bool]) -> Callable[[], bool]:
    calls = {"i": 0}

    def _probe() -> bool:
        value = returns[min(calls["i"], len(returns) - 1)]
        calls["i"] += 1
        return value

    return _probe


def test_returns_when_probe_true_on_first_call() -> None:
    wait_for_network(
        lambda: True,
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=lambda _s: None,
        timeout_s=10.0,
        poll_s=1.0,
    )


def test_polls_until_probe_flips_true() -> None:
    probe = _sequence_probe([False, False, True])
    wait_for_network(
        probe,
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=lambda _s: None,
        timeout_s=10.0,
        poll_s=1.0,
    )


def test_raises_provisioning_failure_past_deadline() -> None:
    with pytest.raises(CategorizedError) as exc:
        wait_for_network(
            lambda: False,
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=3.0,
            poll_s=1.0,
        )
    assert exc.value.category == ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details["domain"] == "kdive-build-x"


def test_timeout_error_carries_timeout_detail_keys() -> None:
    def _detail() -> dict[str, object]:
        return {"probe_stderr": "cut: not found", "probe_stdout": ""}

    with pytest.raises(CategorizedError) as exc:
        wait_for_network(
            lambda: False,
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=3.0,
            poll_s=1.0,
            timeout_detail=_detail,
        )
    assert exc.value.details["probe_stderr"] == "cut: not found"


def test_propagates_categorized_error_raised_by_probe() -> None:
    def _broken_probe() -> bool:
        raise CategorizedError("agent gone", category=ErrorCategory.TRANSPORT_FAILURE)

    with pytest.raises(CategorizedError) as exc:
        wait_for_network(
            _broken_probe,
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=10.0,
            poll_s=1.0,
        )
    assert exc.value.category == ErrorCategory.TRANSPORT_FAILURE
