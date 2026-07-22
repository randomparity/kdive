"""Fleet inventory snapshot + observable gauges (ADR-0190 B + D-gauges).

The reconciler reads a count-by-state + host-capacity snapshot once per pass and the
sync observable-gauge callbacks emit from the cached, frozen snapshot.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation
from kdive.reconciler.fleet import FleetSnapshot, FleetTelemetry, read_fleet_snapshot
from tests.reconciler.conftest import connect

_DT = datetime(2026, 1, 1, tzinfo=UTC)


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


def _gauge_unit(reader: InMemoryMetricReader, name: str) -> str | None:
    """Return the exported unit for gauge ``name`` (asserts the gauge exists)."""
    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    return metric.unit
    raise AssertionError(f"gauge {name!r} not found")


def _telemetry() -> tuple[FleetTelemetry, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    return FleetTelemetry(meter=meter), reader


def test_disabled_fleet_telemetry_is_noop() -> None:
    telemetry = FleetTelemetry.disabled()
    telemetry.refresh(FleetSnapshot.empty())  # no meter; must not raise


def test_gauges_emit_from_the_cached_snapshot() -> None:
    telemetry, reader = _telemetry()
    snapshot = FleetSnapshot(
        inventory={
            "allocations": {"granted": 2, "requested": 1},
            "systems": {"ready": 3},
            "runs": {},
            "debug_sessions": {},
        },
        capacity_used={"local-libvirt": 2},
        capacity_total={"local-libvirt": 5},
    )
    telemetry.refresh(snapshot)

    allocations = _gauge_points(reader, "kdive.allocations")
    assert allocations[(("state", "granted"),)] == 2
    assert allocations[(("state", "requested"),)] == 1
    assert _gauge_points(reader, "kdive.systems")[(("state", "ready"),)] == 3
    assert _gauge_points(reader, "kdive.host.capacity.used")[(("provider", "local-libvirt"),)] == 2
    assert _gauge_points(reader, "kdive.host.capacity.total")[(("provider", "local-libvirt"),)] == 5

    # Every gauge advertises the dimensionless unit "1" (ADR-0190); the exact string is the
    # metric contract, so a dropped/garbled unit must be caught.
    for gauge in (
        "kdive.allocations",
        "kdive.systems",
        "kdive.host.capacity.used",
        "kdive.host.capacity.total",
    ):
        assert _gauge_unit(reader, gauge) == "1"


def test_refresh_swaps_the_cache_and_a_stale_read_keeps_the_last() -> None:
    telemetry, reader = _telemetry()
    telemetry.refresh(
        FleetSnapshot(inventory={"runs": {"running": 1}}, capacity_used={}, capacity_total={})
    )
    assert _gauge_points(reader, "kdive.runs")[(("state", "running"),)] == 1
    telemetry.refresh(
        FleetSnapshot(inventory={"runs": {"running": 4}}, capacity_used={}, capacity_total={})
    )
    assert _gauge_points(reader, "kdive.runs")[(("state", "running"),)] == 4


async def _seed_resource(
    conn: psycopg.AsyncConnection, *, kind: ResourceKind, cap: object
) -> Resource:
    caps: dict[str, object] = {"vcpus": 64, "memory_mb": 65536}
    if cap is not None:
        caps[CONCURRENT_ALLOCATION_CAP_KEY] = cap
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=kind,
            capabilities=caps,
            pool="p",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _seed_alloc(
    conn: psycopg.AsyncConnection, resource_id: object, state: AllocationState
) -> None:
    await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            agent_session=None,
            project="proj",
            resource_id=resource_id,  # ty: ignore[invalid-argument-type]
            state=state,
            lease_expiry=None,
            requested_vcpus=1,
            requested_memory_gb=1,
            requested_disk_gb=1,
            shape="vm",
            pcie_claim=[],
        ),
    )


def test_read_fleet_snapshot_counts_and_caps(migrated_url: str) -> None:
    import asyncio

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, kind=ResourceKind.LOCAL_LIBVIRT, cap=2)
            res2 = await _seed_resource(seed, kind=ResourceKind.LOCAL_LIBVIRT, cap=3)
            # A resource with no valid cap is skipped from the total, not counted as 0-or-raise.
            await _seed_resource(seed, kind=ResourceKind.REMOTE_LIBVIRT, cap=None)
            await _seed_alloc(seed, res.id, AllocationState.GRANTED)
            await _seed_alloc(seed, res.id, AllocationState.ACTIVE)
            await _seed_alloc(seed, res2.id, AllocationState.RELEASED)  # terminal, not occupying

        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            snapshot = await read_fleet_snapshot(conn)

        # Inventory is zero-filled across the full enum.
        assert snapshot.inventory["allocations"]["granted"] == 1
        assert snapshot.inventory["allocations"]["active"] == 1
        assert snapshot.inventory["allocations"]["released"] == 1
        assert snapshot.inventory["allocations"]["failed"] == 0  # zero-filled
        assert set(snapshot.inventory) == {"allocations", "systems", "runs", "debug_sessions"}
        assert set(snapshot.inventory["systems"]) == {s.value for s in SystemState}
        assert set(snapshot.inventory["runs"]) == {s.value for s in RunState}
        assert set(snapshot.inventory["debug_sessions"]) == {s.value for s in DebugSessionState}

        # Capacity: 2 occupying (granted+active) on local-libvirt; total = 2+3 (remote cap skipped).
        assert snapshot.capacity_used["local-libvirt"] == 2
        assert snapshot.capacity_total["local-libvirt"] == 5
        assert "remote-libvirt" not in snapshot.capacity_total  # no valid cap → skipped

    asyncio.run(_run())


def test_capacity_total_skips_capless_then_continues_to_later_resources(migrated_url: str) -> None:
    # A cap-less resource is skipped but the sum must CONTINUE to the resources that follow it.
    # The cap-less resource is seeded first so a `break` regression would drop the later caps.
    import asyncio

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_resource(seed, kind=ResourceKind.REMOTE_LIBVIRT, cap=None)  # skipped, first
            await _seed_resource(seed, kind=ResourceKind.LOCAL_LIBVIRT, cap=4)
            await _seed_resource(seed, kind=ResourceKind.LOCAL_LIBVIRT, cap=6)
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            snapshot = await read_fleet_snapshot(conn)
        # Both local caps summed despite the earlier cap-less skip; break would drop them.
        assert snapshot.capacity_total["local-libvirt"] == 10

    asyncio.run(_run())


def test_no_capless_warning_when_all_resources_have_valid_caps(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    # `skipped` starts at 0, so an all-valid fleet emits NO cap-less warning (a nonzero start
    # would warn on a clean fleet every pass).
    import asyncio

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_resource(seed, kind=ResourceKind.LOCAL_LIBVIRT, cap=2)
            await _seed_resource(seed, kind=ResourceKind.LOCAL_LIBVIRT, cap=3)
        caplog.set_level(logging.WARNING, logger="kdive.reconciler.fleet")
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await read_fleet_snapshot(conn)
        assert not [r for r in caplog.records if "no valid allocation cap" in r.getMessage()]

    asyncio.run(_run())
