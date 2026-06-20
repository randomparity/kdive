"""Fleet inventory snapshot + observable gauges (ADR-0190 B + D-gauges).

The reconciler reads a count-by-state snapshot of the four lifecycle objects plus per-provider
host capacity once per pass (``read_fleet_snapshot``) and caches it on :class:`FleetTelemetry`.
The observable-gauge callbacks emit from that cached, frozen snapshot — an OTel observable
callback is synchronous and cannot ``await`` the async pool, so the read happens on the pass
(its own connection) and the scrape just reads the cache. This mirrors the worker's queue-depth
caching (``WorkerTelemetry.observe_queue_depth``).

``state`` is bounded by each object's state enum (zero-filled, so a state at 0 still emits a
series); ``provider`` is bounded by ``ResourceKind`` (the resource's ``kind`` is the provider
family). No per-object / per-tenant label travels (ADR-0090 §4).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from psycopg import AsyncConnection, sql

from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resource_capabilities import ResourceCapabilities

if TYPE_CHECKING:
    from opentelemetry.metrics import CallbackOptions, Meter, Observation

_log = logging.getLogger(__name__)

#: (metric-object name == table name, state enum) for each lifecycle inventory gauge. The
#: object name is both the gauge suffix (``kdive.<object>``) and the table queried.
_INVENTORY: tuple[tuple[str, type[StrEnum]], ...] = (
    ("allocations", AllocationState),
    ("systems", SystemState),
    ("runs", RunState),
    ("debug_sessions", DebugSessionState),
)

#: Allocation states that occupy a host-cap slot (ADR-0069). A queued ``requested`` row holds
#: only a queue position and is excluded. Mirrors admission's ``OCCUPYING``.
_OCCUPYING_VALUES: tuple[str, ...] = (
    AllocationState.GRANTED.value,
    AllocationState.ACTIVE.value,
    AllocationState.RELEASING.value,
)


@dataclass(frozen=True, slots=True)
class FleetSnapshot:
    """An immutable per-pass view of the fleet (ADR-0190 B + D-gauges).

    Frozen on purpose: :meth:`FleetTelemetry.refresh` rebinds the cache to one new reference
    so a scrape-thread gauge callback reading the reference under the GIL can never observe a
    half-updated snapshot.
    """

    inventory: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    capacity_used: Mapping[str, int] = field(default_factory=dict)
    capacity_total: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> FleetSnapshot:
        """Return the pre-first-pass snapshot (no series emitted until the first refresh)."""
        return cls()


async def read_fleet_snapshot(conn: AsyncConnection) -> FleetSnapshot:
    """Read count-by-state for each lifecycle object + per-provider host capacity.

    Each object's counts are zero-filled across its full state enum so a state that drops to
    zero still emits a ``0`` series (a grouped ``COUNT(*)`` only returns states with rows).
    Capacity ``used`` is occupying allocations per provider; ``total`` sums the advertised
    ``concurrent_allocation_cap`` per provider, read through the typed capability view and
    skipping (with a one-line warn) any resource whose cap is absent/invalid.
    """
    inventory: dict[str, Mapping[str, int]] = {}
    for table, enum in _INVENTORY:
        inventory[table] = await _count_by_state(conn, table, enum)
    capacity_used = await _capacity_used(conn)
    capacity_total = await _capacity_total(conn)
    return FleetSnapshot(
        inventory=inventory, capacity_used=capacity_used, capacity_total=capacity_total
    )


async def _count_by_state(conn: AsyncConnection, table: str, enum: type[StrEnum]) -> dict[str, int]:
    counts = {member.value: 0 for member in enum}  # zero-fill the full enum
    # `table` is a fixed literal from `_INVENTORY` (never user input); compose it as an
    # identifier so the query stays injection-safe and typed (no raw f-string into execute).
    query = sql.SQL("SELECT state, count(*) FROM {} GROUP BY state").format(sql.Identifier(table))
    async with conn.cursor() as cur:
        await cur.execute(query)
        for state, count in await cur.fetchall():
            counts[str(state)] = int(count)
    return counts


async def _capacity_used(conn: AsyncConnection) -> dict[str, int]:
    used: dict[str, int] = {}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT r.kind, count(*) FROM allocations a JOIN resources r ON a.resource_id = r.id "
            "WHERE a.state = ANY(%s) GROUP BY r.kind",
            (list(_OCCUPYING_VALUES),),
        )
        for provider, count in await cur.fetchall():
            used[str(provider)] = int(count)
    return used


async def _capacity_total(conn: AsyncConnection) -> dict[str, int]:
    total: dict[str, int] = {}
    async with conn.cursor() as cur:
        await cur.execute("SELECT id, kind, capabilities FROM resources")
        rows = await cur.fetchall()
    skipped = 0
    for _resource_id, provider, capabilities in rows:
        cap = ResourceCapabilities.from_mapping(capabilities or {}).allocation_cap()
        if cap is None:
            skipped += 1  # a cap-less resource can't be allocated; excluded from the total
            continue
        total[str(provider)] = total.get(str(provider), 0) + cap
    if skipped:
        # One aggregate line per pass, not per resource — a steady-state misconfig must not
        # flood the log every reconcile interval.
        _log.warning(
            "fleet snapshot: %d resource(s) have no valid allocation cap; excluded", skipped
        )
    return total


class FleetTelemetry:
    """Register the inventory + host-capacity observable gauges over a cached snapshot.

    Args:
        meter: The meter (the facade's reconciler ``MeterProvider``) the gauges are made on.
    """

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._snapshot: FleetSnapshot = FleetSnapshot.empty()
        for table, _enum in _INVENTORY:
            meter.create_observable_gauge(
                f"kdive.{table}",
                callbacks=[self._inventory_callback(table)],
                unit="1",
                description=f"{table} grouped by lifecycle state (live count).",
            )
        meter.create_observable_gauge(
            "kdive.host.capacity.used",
            callbacks=[self._capacity_callback(used=True)],
            unit="1",
            description="Host-cap slots occupied per provider.",
        )
        meter.create_observable_gauge(
            "kdive.host.capacity.total",
            callbacks=[self._capacity_callback(used=False)],
            unit="1",
            description="Advertised host-cap slots per provider.",
        )

    @classmethod
    def disabled(cls) -> FleetTelemetry:
        """Return a no-op telemetry (no meter/gauges) for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        instance._snapshot = FleetSnapshot.empty()
        return instance

    def refresh(self, snapshot: FleetSnapshot) -> None:
        """Rebind the cache to ``snapshot`` (one immutable reference; no-op when disabled)."""
        if self._enabled:
            self._snapshot = snapshot

    def _inventory_callback(self, table: str):  # noqa: ANN202 - OTel callback type
        def _observe(_options: CallbackOptions) -> Iterable[Observation]:
            from opentelemetry.metrics import Observation

            counts = self._snapshot.inventory.get(table, {})
            return [Observation(count, {"state": state}) for state, count in counts.items()]

        return _observe

    def _capacity_callback(self, *, used: bool):  # noqa: ANN202 - OTel callback type
        def _observe(_options: CallbackOptions) -> Iterable[Observation]:
            from opentelemetry.metrics import Observation

            source = self._snapshot.capacity_used if used else self._snapshot.capacity_total
            return [Observation(n, {"provider": provider}) for provider, n in source.items()]

        return _observe
