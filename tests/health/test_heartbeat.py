"""Tests for the affirmative liveness heartbeat."""

from __future__ import annotations

import asyncio

from kdive.health.heartbeat import Heartbeat, tick_until_stop


def test_heartbeat_starts_live() -> None:
    now = {"value": 100.0}
    heartbeat = Heartbeat(stale_after=10.0, now=lambda: now["value"])

    assert heartbeat.is_live() is True


def test_heartbeat_goes_stale_after_threshold() -> None:
    now = {"value": 100.0}
    heartbeat = Heartbeat(stale_after=10.0, now=lambda: now["value"])

    now["value"] = 109.999
    assert heartbeat.is_live() is True

    now["value"] = 110.0
    assert heartbeat.is_live() is False


def test_tick_refreshes_liveness_window() -> None:
    now = {"value": 100.0}
    heartbeat = Heartbeat(stale_after=10.0, now=lambda: now["value"])

    now["value"] = 109.0
    heartbeat.tick()
    now["value"] = 118.0
    assert heartbeat.is_live() is True

    now["value"] = 119.0
    assert heartbeat.is_live() is False


def test_ticker_stops_without_a_final_tick() -> None:
    async def _run() -> None:
        ticks = 0
        stop = asyncio.Event()

        class _CountingHeartbeat:
            def tick(self) -> None:
                nonlocal ticks
                ticks += 1

        async def _sleep_until_stop(_stop: asyncio.Event, _interval: float) -> None:
            _stop.set()

        await tick_until_stop(_CountingHeartbeat(), stop, 1.0, _sleep_until_stop)
        assert ticks == 1

    asyncio.run(_run())
