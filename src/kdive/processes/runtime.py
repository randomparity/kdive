"""Shared runtime wrapper for long-running KDIVE processes."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

if TYPE_CHECKING:
    from kdive.health.heartbeat import Heartbeat
    from kdive.health.probe import HealthProbe
    from kdive.observability.facade import Telemetry
    from kdive.security.secrets.secret_registry import SecretRegistry

HEARTBEAT_TICK_SECONDS = 1.0
HEARTBEAT_STALE_SECONDS = 10.0

# How long startup waits for the pool's first connection before giving up (ADR-0449).
# Ten seconds sits in the gap between the two budgets that bound it: an order of magnitude
# above the 1-second acquire budget `UsageTrackingMiddleware` uses, so a merely cold or
# contended backend can no longer land inside it; and well under the Helm chart's liveness
# budget (`initialDelaySeconds: 5` + 3 x `periodSeconds: 10` = 35s), so a genuinely
# unreachable backend makes the process report its own failure rather than being killed
# mid-open by a probe that cannot say why.
POOL_OPEN_TIMEOUT_SECONDS = 10.0

type ProbeBuilder = Callable[[AsyncConnectionPool], HealthProbe]
type ProcessBody = Callable[[AsyncConnectionPool, Heartbeat, HealthProbe], Awaitable[None]]


async def run_process_runtime(
    *,
    process: str,
    pool: AsyncConnectionPool,
    secret_registry: SecretRegistry,
    telemetry: Telemetry,
    heartbeat_stale_after: float,
    probe_builder: ProbeBuilder,
    body: ProcessBody,
    tick_heartbeat: bool = False,
) -> None:
    from kdive.health.aux_bind import resolve_health_bind
    from kdive.health.aux_listener import build_aux_app, serve_aux
    from kdive.health.heartbeat import Heartbeat

    tasks: list[asyncio.Task[None]] = []
    try:
        # `wait=True` so the first connect completes here rather than in a background task
        # (ADR-0449). `open()`'s default returns before any connection exists, which hands
        # `body` a pool reporting zero available connections — and the server's
        # `UsageTrackingMiddleware` acquires with a 1-second budget and swallows the
        # resulting `PoolTimeout` into a WARNING, silently dropping the row (#1535, #1527).
        # Inside the `try` so an unreachable backend still runs the teardown below.
        await pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT_SECONDS)
        heartbeat = Heartbeat(stale_after=heartbeat_stale_after)
        probe = probe_builder(pool)
        aux_host, aux_port = resolve_health_bind(process)
        aux_app = build_aux_app(
            heartbeat=heartbeat, probe=probe, metric_reader=telemetry.scrape_reader
        )
        tasks.append(asyncio.create_task(serve_aux(aux_app, host=aux_host, port=aux_port)))
        if tick_heartbeat:
            tasks.append(asyncio.create_task(tick_heartbeat_loop(heartbeat)))
        await body(pool, heartbeat, probe)
    finally:
        await cancel(*tasks)
        secret_registry.clear()
        await pool.close()


async def cancel(*tasks: asyncio.Task[None]) -> None:
    """Cancel and await aux background tasks before shared resources are torn down."""
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def tick_heartbeat_loop(heartbeat: Heartbeat) -> None:
    while True:
        heartbeat.tick()
        await asyncio.sleep(HEARTBEAT_TICK_SECONDS)


def install_stop() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    return stop


def readiness(probe: HealthProbe) -> Callable[[], Awaitable[bool]]:
    async def ready() -> bool:
        return (await probe.check()).ready

    return ready
