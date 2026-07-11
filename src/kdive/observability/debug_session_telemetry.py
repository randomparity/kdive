"""Debug-session duration histogram (ADR-0191 H3).

Records the wall-clock lifetime of a DebugSession at both the clean close (``end_session``,
outcomes ``ok``/``error``) and the reconciler reap (outcome ``reaped``). One process-global
instance is built per process from that process's meter so server and reconciler aggregate
to the same instrument name at the collector — the same pattern as ``kdive.errors``.
``disabled()`` is the no-op default for tests and un-instrumented runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from opentelemetry.metrics import Histogram, Meter

type DebugSessionOutcome = Literal["ok", "error", "reaped"]

_DURATION_BUCKETS = (1.0, 10.0, 60.0, 300.0, 1800.0, 3600.0, 14400.0)


class DebugSessionTelemetry:
    """Record debug-session duration (ADR-0191 H3)."""

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._duration: Histogram = meter.create_histogram(
            "kdive.debug.session.duration",
            unit="s",
            description="Debug-session wall-clock duration, by transport and outcome.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )

    @classmethod
    def disabled(cls) -> DebugSessionTelemetry:
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    def record(self, transport: str, outcome: DebugSessionOutcome, seconds: float) -> None:
        if not self._enabled or seconds < 0.0:
            return
        self._duration.record(seconds, {"transport": transport, "outcome": outcome})
