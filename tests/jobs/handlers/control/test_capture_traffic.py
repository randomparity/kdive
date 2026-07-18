"""Worker handler for capture_traffic: poll loop + snapshot/store (ADR-0384)."""

from __future__ import annotations

import asyncio

from kdive.jobs.handlers.control.capture_traffic import LoopResult, run_capture_loop


def _run(sizes, *, canceled_at=None, max_bytes=10_000, max_polls=5):
    calls = {"n": 0}

    async def stat():
        i = min(calls["n"], len(sizes) - 1)
        return sizes[i]

    async def sleep(_seconds):
        calls["n"] += 1

    async def canceled():
        return canceled_at is not None and calls["n"] >= canceled_at

    return asyncio.run(
        run_capture_loop(
            stat=stat, sleep=sleep, canceled=canceled, max_bytes=max_bytes, max_polls=max_polls
        )
    )


def test_loop_stops_at_duration() -> None:
    result = _run([100, 200, 300], max_polls=3)
    assert result == LoopResult(truncated=False, canceled=False)


def test_loop_stops_at_max_bytes() -> None:
    result = _run([100, 5000, 20000], max_bytes=10_000, max_polls=9)
    assert result.truncated is True
    assert result.canceled is False


def test_loop_stops_on_cancel() -> None:
    result = _run([100, 100, 100], canceled_at=2, max_polls=9)
    assert result.canceled is True
    assert result.truncated is False
