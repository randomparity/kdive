"""Build-host lease/capacity/reachability snapshot + observable gauges (ADR-0191 G2/G3).

Reads a per-pass snapshot of the ``build_hosts`` table and the ``build_host_leases``
join, then exposes it as three observable gauges keyed by host name. The ``build_host``
label is bounded by the operator's ``build_hosts`` table (a deployment-bounded set, not
an enum — the documented non-enum exception in ADR-0191 §1).

Like :class:`~kdive.reconciler.fleet.FleetTelemetry`, the OTel observable callbacks are
synchronous and cannot await the async pool, so the snapshot is read on the reconciler
pass (its own connection) and the scrape reads from the frozen cache.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from psycopg import AsyncConnection

if TYPE_CHECKING:
    from opentelemetry.metrics import CallbackOptions, Meter, Observation

_LEASES_QUERY = (
    "SELECT h.name, count(l.run_id) "
    "FROM build_hosts h "
    "LEFT JOIN build_host_leases l ON l.build_host_id = h.id "
    "GROUP BY h.name"
)

_CAP_REACHABLE_QUERY = "SELECT name, max_concurrent, state FROM build_hosts"


@dataclass(frozen=True, slots=True)
class BuildHostSnapshot:
    """An immutable per-pass view of the build-host fleet (ADR-0191 G2/G3).

    Frozen on purpose: :meth:`BuildHostTelemetry.refresh` rebinds the cache to one new
    reference so a scrape-thread gauge callback reading the reference under the GIL can
    never observe a half-updated snapshot.

    Attributes:
        leases: Active lease count per host name (LEFT JOIN, so 0-lease hosts are present).
        capacity: ``max_concurrent`` per host name.
        reachable: 1.0 if ``state='ready'``, 0.0 if ``'unreachable'``, per host name.
    """

    leases: Mapping[str, int] = field(default_factory=dict)
    capacity: Mapping[str, int] = field(default_factory=dict)
    reachable: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> BuildHostSnapshot:
        """Return the pre-first-pass snapshot (no series emitted until the first refresh)."""
        return cls()


async def read_build_host_snapshot(conn: AsyncConnection) -> BuildHostSnapshot:
    """Read lease counts, capacity, and reachability from the build-host tables.

    Uses a LEFT JOIN for leases so a host with no active leases still emits a 0 series.

    Args:
        conn: An open async psycopg connection (read-only, no transaction required).

    Returns:
        A frozen :class:`BuildHostSnapshot` reflecting the current state.
    """
    leases: dict[str, int] = {}
    async with conn.cursor() as cur:
        await cur.execute(_LEASES_QUERY)
        for name, count in await cur.fetchall():
            leases[str(name)] = int(count)

    capacity: dict[str, int] = {}
    reachable: dict[str, float] = {}
    async with conn.cursor() as cur:
        await cur.execute(_CAP_REACHABLE_QUERY)
        for name, max_concurrent, state in await cur.fetchall():
            capacity[str(name)] = int(max_concurrent)
            reachable[str(name)] = 1.0 if str(state) == "ready" else 0.0

    return BuildHostSnapshot(leases=leases, capacity=capacity, reachable=reachable)


class BuildHostTelemetry:
    """Register build-host lease/capacity/reachability observable gauges over a cached snapshot.

    Mirrors :class:`~kdive.reconciler.fleet.FleetTelemetry`: the gauge callbacks are
    registered at construction time; :meth:`refresh` rebinds the frozen snapshot cache.

    Args:
        meter: The reconciler meter; gauges are made on this meter.
    """

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._snapshot: BuildHostSnapshot = BuildHostSnapshot.empty()
        meter.create_observable_gauge(
            "kdive.build_host.leases",
            callbacks=[self._leases_callback],
            unit="1",
            description="Active build-host lease count per host.",
        )
        meter.create_observable_gauge(
            "kdive.build_host.capacity",
            callbacks=[self._capacity_callback],
            unit="1",
            description="Maximum concurrent build leases per host (max_concurrent).",
        )
        meter.create_observable_gauge(
            "kdive.build_host.reachable",
            callbacks=[self._reachable_callback],
            unit="1",
            description="1.0 if the host is state=ready, 0.0 if state=unreachable.",
        )

    @classmethod
    def disabled(cls) -> BuildHostTelemetry:
        """Return a no-op telemetry (no meter/gauges) for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        instance._snapshot = BuildHostSnapshot.empty()
        return instance

    def refresh(self, snapshot: BuildHostSnapshot) -> None:
        """Rebind the cache to ``snapshot`` (one immutable reference; no-op when disabled)."""
        if self._enabled:
            self._snapshot = snapshot

    def _leases_callback(self, _options: CallbackOptions) -> Iterable[Observation]:
        from opentelemetry.metrics import Observation

        return [
            Observation(count, {"build_host": name})
            for name, count in self._snapshot.leases.items()
        ]

    def _capacity_callback(self, _options: CallbackOptions) -> Iterable[Observation]:
        from opentelemetry.metrics import Observation

        return [
            Observation(cap, {"build_host": name}) for name, cap in self._snapshot.capacity.items()
        ]

    def _reachable_callback(self, _options: CallbackOptions) -> Iterable[Observation]:
        from opentelemetry.metrics import Observation

        return [
            Observation(val, {"build_host": name}) for name, val in self._snapshot.reachable.items()
        ]
