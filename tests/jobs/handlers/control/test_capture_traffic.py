"""Worker handler for capture_traffic: poll loop + snapshot/store (ADR-0385)."""

from __future__ import annotations

import asyncio

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
