"""Tests for debug-session reaper telemetry wiring (ADR-0191 H3).

Verifies that ``repair_dead_sessions`` records a ``reaped`` duration point for each
detached session.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import DebugSessionState
from kdive.observability.debug_session_telemetry import DebugSessionTelemetry
from kdive.providers.core.transport_reset import NullResetter
from kdive.reconciler.repairs.debug_sessions import repair_dead_sessions
from tests.reconciler.conftest import (
    connect,
    run_repair,
    seed_debug_session,
    seed_run,
    seed_system,
)


def _points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    out: list[Any] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


def _make_telemetry() -> tuple[InMemoryMetricReader, DebugSessionTelemetry]:
    reader = InMemoryMetricReader()
    tel = DebugSessionTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    return reader, tel


def test_reaped_session_records_duration(migrated_url: str) -> None:
    """A stale session reaped by the reconciler emits a duration point with outcome='reaped'."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await seed_debug_session(
                seed,
                run_id,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=1),
                transport="gdbstub",
            )

        reader, tel = _make_telemetry()
        resetter = NullResetter()

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                lambda conn: repair_dead_sessions(conn, timedelta(minutes=2), resetter, tel),
            )

        assert count == 1
        pts = _points(reader, "kdive.debug.session.duration")
        assert pts, "no duration point emitted for reaped session"
        assert pts[0].attributes["outcome"] == "reaped"
        assert pts[0].attributes["transport"] == "gdbstub"
        assert pts[0].sum >= 0.0

    asyncio.run(_run())


def test_reaped_multiple_sessions_each_get_a_point(migrated_url: str) -> None:
    """Each reaped session produces its own duration data point."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await seed_debug_session(
                seed,
                run_id,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=2),
                transport="gdbstub",
            )
            await seed_debug_session(
                seed,
                run_id,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=3),
                transport="drgn-live",
            )

        reader, tel = _make_telemetry()
        resetter = NullResetter()

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                lambda conn: repair_dead_sessions(conn, timedelta(minutes=2), resetter, tel),
            )

        assert count == 2
        pts = _points(reader, "kdive.debug.session.duration")
        assert len(pts) == 2
        outcomes = {p.attributes["outcome"] for p in pts}
        assert outcomes == {"reaped"}

    asyncio.run(_run())


def test_disabled_telemetry_is_noop(migrated_url: str) -> None:
    """repair_dead_sessions with disabled telemetry still works and does not emit."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await seed_debug_session(
                seed,
                run_id,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=1),
            )

        reader = InMemoryMetricReader()
        resetter = NullResetter()

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                # default telemetry param (disabled)
                lambda conn: repair_dead_sessions(conn, timedelta(minutes=2), resetter),
            )

        assert count == 1
        assert not _points(reader, "kdive.debug.session.duration")

    asyncio.run(_run())
