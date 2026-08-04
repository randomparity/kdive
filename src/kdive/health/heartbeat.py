"""The affirmative ``/livez`` loop heartbeat (ADR-0090 §5).

``/livez`` is an *affirmative* liveness signal, not liveness-by-timeout, and it tracks
the **loop, not the work unit**. The owning loop bumps :meth:`Heartbeat.tick` at its
scheduling/poll granularity (it woke, is dequeuing, has not deadlocked) — *not* at job
completion, since kdive jobs legitimately run for minutes. ``/livez`` is live while the
last tick is within :attr:`stale_after` seconds; a genuinely stuck job is caught by
job-duration metrics and per-job timeouts, not by liveness.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, SupportsFloat


class _Ticker(Protocol):
    def tick(self) -> None: ...


class Heartbeat:
    """A monotonic last-tick timestamp the aux ``/livez`` handler reads.

    Args:
        stale_after: Seconds after the last :meth:`tick` at which liveness goes stale.
        now: Monotonic clock (injected for tests); defaults to :func:`time.monotonic`.
    """

    def __init__(
        self, *, stale_after: float, now: Callable[[], SupportsFloat] = time.monotonic
    ) -> None:
        self._stale_after = stale_after
        self._now = now
        self._last_tick = self._read_now()

    def _read_now(self) -> float:
        return float(self._now())

    def tick(self) -> None:
        """Record that the owning loop made a scheduling pass (woke and is progressing)."""
        self._last_tick = self._read_now()

    def is_live(self) -> bool:
        """Return whether the last tick is within :attr:`stale_after` seconds."""
        return (self._read_now() - self._last_tick) < self._stale_after


async def tick_until_stop(
    heartbeat: _Ticker,
    stop: asyncio.Event,
    interval: float,
    sleep_until_stop: Callable[[asyncio.Event, float], Awaitable[None]],
) -> None:
    """Tick immediately and then after each stop-aware interval until stopped or cancelled."""
    heartbeat.tick()
    while not stop.is_set():
        await sleep_until_stop(stop, interval)
        if stop.is_set():
            break
        heartbeat.tick()
