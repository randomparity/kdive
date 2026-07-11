"""vmcore-capture duration + bytes telemetry (ADR-0191 H1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from opentelemetry.metrics import Histogram, Meter

type CaptureOutcome = Literal["ok", "error"]

_DURATION_BUCKETS = (1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0)
_BYTE_BUCKETS = (1e6, 1e7, 1e8, 5e8, 1e9, 5e9)


class CaptureTelemetry:
    """Record vmcore capture duration + raw byte size (ADR-0191 H1)."""

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._duration: Histogram = meter.create_histogram(
            "kdive.vmcore.capture.duration",
            unit="s",
            description="vmcore capture wall-clock duration, by method and provider.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )
        self._bytes: Histogram = meter.create_histogram(
            "kdive.vmcore.capture.bytes",
            unit="By",
            description="Raw vmcore size captured, by method and provider.",
            explicit_bucket_boundaries_advisory=list(_BYTE_BUCKETS),
        )

    @classmethod
    def disabled(cls) -> CaptureTelemetry:
        """Return a no-op telemetry for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    def record(
        self,
        capture_method: str,
        provider: str,
        outcome: CaptureOutcome,
        *,
        seconds: float,
        size_bytes: int | None = None,
    ) -> None:
        """Record a capture's duration, and its raw byte size on success."""
        if not self._enabled:
            return
        labels = {"capture_method": capture_method, "provider": provider}
        self._duration.record(seconds, {**labels, "outcome": outcome})
        if size_bytes is not None:
            self._bytes.record(size_bytes, labels)
