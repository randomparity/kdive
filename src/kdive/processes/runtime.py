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
    await pool.open()
    try:
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
