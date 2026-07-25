"""Shared runtime wrapper for long-running KDIVE processes."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool, PoolTimeout

from kdive.config.core_settings import DATABASE_URL
from kdive.domain.errors import CategorizedError, ErrorCategory

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
# contended backend can no longer land inside it; and under the Helm chart's liveness
# budget, which kills the container at t~=25s (`initialDelaySeconds: 5` then
# `periodSeconds: 10` x the default `failureThreshold: 3` -> probes at t=5, 15, 25), so a
# genuinely unreachable backend makes the process report its own failure rather than being
# killed mid-open by a probe that cannot say why. Sizing this up means budgeting against
# that 25s *minus* everything the process does before the open — interpreter start,
# imports, `config.validate`, and `init_telemetry` — none of which this constant covers.
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
        await open_pool_or_fail(pool)
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


async def open_pool_or_fail(pool: AsyncConnectionPool) -> None:
    """Open ``pool`` with its first connection established, or fail with a named cause.

    ``wait=True`` so the first connect completes here rather than in a background task
    (ADR-0449). ``open()``'s default returns before any connection exists, which hands the
    process body a pool reporting zero available connections — and the server's
    ``UsageTrackingMiddleware`` acquires with a 1-second budget and swallows the resulting
    ``PoolTimeout`` into a WARNING, silently dropping the row (#1535, #1527).

    An unreachable backend is a routine startup condition, not a bug, so it is translated
    into a ``CategorizedError``: that is what ``__main__`` handles, and it is the difference
    between an ERROR record on the structured log floor with a stable exit code (ADR-0090)
    and a bare traceback that a deployment scraping JSON cannot see at all.

    Raises:
        CategorizedError: the pool had no connection within
            :data:`POOL_OPEN_TIMEOUT_SECONDS` (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
    """
    try:
        await pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT_SECONDS)
    except PoolTimeout as exc:
        raise CategorizedError(
            f"database unreachable: no connection within {POOL_OPEN_TIMEOUT_SECONDS:.0f}s "
            f"of process start",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "timeout_seconds": POOL_OPEN_TIMEOUT_SECONDS,
                "variable": DATABASE_URL.name,
                # The conninfo carries a password; `database_url()` is not echoed here and
                # the redactor covers the rest.
                "suggest": (
                    "check that Postgres is reachable and that "
                    f"{DATABASE_URL.name} is correct, then restart"
                ),
            },
        ) from exc


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
