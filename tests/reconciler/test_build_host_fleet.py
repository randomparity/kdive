"""Build-host snapshot gauges (ADR-0191 G2/G3).

Tests the ``BuildHostSnapshot`` read function (DB-backed, skips without Docker) and the
``BuildHostTelemetry`` observable-gauge callbacks (in-memory only, no DB required).
Mirrors ``tests/reconciler/test_fleet.py`` in structure and fixture usage.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from psycopg_pool import AsyncConnectionPool

from kdive.reconciler.build_host_fleet import (
    BuildHostSnapshot,
    BuildHostTelemetry,
    read_build_host_snapshot,
)
from tests.reconciler.conftest import connect

# ---------------------------------------------------------------------------
# Gauge-callback tests (no DB required)
# ---------------------------------------------------------------------------


def _telemetry() -> tuple[BuildHostTelemetry, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    return BuildHostTelemetry(meter=meter), reader


def _gauge_points(
    reader: InMemoryMetricReader, name: str
) -> dict[tuple[tuple[str, str], ...], float]:
    data = reader.get_metrics_data()
    assert data is not None
    points: dict[tuple[tuple[str, str], ...], float] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    value = getattr(point, "value", None)
                    if value is None:
                        continue
                    attrs = point.attributes or {}
                    key = tuple(sorted((str(k), str(v)) for k, v in attrs.items()))
                    points[key] = value
    return points


def test_disabled_build_host_telemetry_is_noop() -> None:
    telemetry = BuildHostTelemetry.disabled()
    telemetry.refresh(BuildHostSnapshot.empty())  # no meter; must not raise


def test_gauges_emit_leases_capacity_reachable_from_snapshot() -> None:
    telemetry, reader = _telemetry()
    snapshot = BuildHostSnapshot(
        leases={"alpha": 1, "beta": 0},
        capacity={"alpha": 4, "beta": 2},
        reachable={"alpha": 1.0, "beta": 0.0},
    )
    telemetry.refresh(snapshot)

    leases = _gauge_points(reader, "kdive.build_host.leases")
    assert leases[(("build_host", "alpha"),)] == 1
    assert leases[(("build_host", "beta"),)] == 0

    capacity = _gauge_points(reader, "kdive.build_host.capacity")
    assert capacity[(("build_host", "alpha"),)] == 4
    assert capacity[(("build_host", "beta"),)] == 2

    reachable = _gauge_points(reader, "kdive.build_host.reachable")
    assert reachable[(("build_host", "alpha"),)] == 1.0
    assert reachable[(("build_host", "beta"),)] == 0.0


def test_refresh_swaps_cache_and_no_refresh_keeps_last() -> None:
    telemetry, reader = _telemetry()
    telemetry.refresh(
        BuildHostSnapshot(
            leases={"alpha": 1},
            capacity={"alpha": 4},
            reachable={"alpha": 1.0},
        )
    )
    assert _gauge_points(reader, "kdive.build_host.leases")[(("build_host", "alpha"),)] == 1

    telemetry.refresh(
        BuildHostSnapshot(
            leases={"alpha": 3},
            capacity={"alpha": 4},
            reachable={"alpha": 1.0},
        )
    )
    assert _gauge_points(reader, "kdive.build_host.leases")[(("build_host", "alpha"),)] == 3


# ---------------------------------------------------------------------------
# DB-backed snapshot test (skips without Docker / migrated_url fixture)
# ---------------------------------------------------------------------------


async def _seed_build_host(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    state: str = "ready",
    max_concurrent: int = 4,
) -> str:
    """Insert a minimal ssh build_host; return its name."""
    host_id = uuid4()
    await conn.execute(
        "INSERT INTO build_hosts (id, name, kind, address, ssh_credential_ref, "
        "    workspace_root, max_concurrent, state) "
        "VALUES (%s, %s, 'ssh', '10.0.0.1', 'cred-ref', '/build', %s, %s)",
        (host_id, name, max_concurrent, state),
    )
    return name


async def _seed_lease(conn: psycopg.AsyncConnection, name: str) -> None:
    """Insert a build_host_leases row for a fake run on the named host."""
    run_id = uuid4()
    cur = await conn.execute(
        "SELECT id FROM build_hosts WHERE name = %s",
        (name,),
    )
    row = await cur.fetchone()
    assert row is not None
    host_id = row[0]
    await conn.execute(
        "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
        (run_id, host_id),
    )


def test_read_build_host_snapshot_counts_leases_capacity_reachable(
    migrated_url: str,
) -> None:
    """DB test: two hosts, one lease, one unreachable — snapshot reflects all three axes."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            # "alpha": ready, max=4, 1 lease
            await _seed_build_host(seed, name="alpha-test", state="ready", max_concurrent=4)
            await _seed_lease(seed, "alpha-test")
            # "beta": unreachable, max=2, 0 leases
            await _seed_build_host(seed, name="beta-test", state="unreachable", max_concurrent=2)

        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            snap = await read_build_host_snapshot(conn)

        # worker-local is seeded by the migration; filter to test-seeded names
        assert snap.leases.get("alpha-test") == 1
        assert snap.leases.get("beta-test") == 0
        assert snap.capacity.get("alpha-test") == 4
        assert snap.capacity.get("beta-test") == 2
        assert snap.reachable.get("alpha-test") == 1.0
        assert snap.reachable.get("beta-test") == 0.0

    asyncio.run(_run())
