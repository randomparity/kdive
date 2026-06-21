"""Tests for DebugSessionTelemetry (ADR-0191 H3)."""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.mcp.tools.debug.debug_session_telemetry import DebugSessionTelemetry


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


def _metric(reader: InMemoryMetricReader, name: str) -> Any:
    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    return m
    raise AssertionError(f"no metric named {name!r} emitted")


def test_histogram_metadata_matches_the_instrument_contract() -> None:
    # Name/unit/description/buckets are the instrument's wire contract: a collector
    # aggregates by exactly this name, charts the unit, and pre-sizes the advisory buckets.
    reader = InMemoryMetricReader()
    tel = DebugSessionTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tel.record("gdbstub", "ok", 12.0)
    metric = _metric(reader, "kdive.debug.session.duration")
    assert metric.unit == "s"
    assert metric.description == "Debug-session wall-clock duration, by transport and outcome."
    assert metric.data.data_points[0].explicit_bounds == (
        1.0,
        10.0,
        60.0,
        300.0,
        1800.0,
        3600.0,
        14400.0,
    )


def test_record_emits_duration_point_with_transport_and_outcome() -> None:
    reader = InMemoryMetricReader()
    tel = DebugSessionTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tel.record("gdbstub", "ok", 12.0)
    pts = _points(reader, "kdive.debug.session.duration")
    assert pts, "no duration point emitted"
    assert pts[0].attributes["transport"] == "gdbstub"
    assert pts[0].attributes["outcome"] == "ok"


def test_record_reaped_outcome_emits_point() -> None:
    reader = InMemoryMetricReader()
    tel = DebugSessionTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tel.record("drgn-live", "reaped", 3600.0)
    pts = _points(reader, "kdive.debug.session.duration")
    assert pts, "no duration point emitted for reaped outcome"
    assert pts[0].attributes["outcome"] == "reaped"
    assert pts[0].attributes["transport"] == "drgn-live"


def test_disabled_is_noop() -> None:
    reader = InMemoryMetricReader()
    tel = DebugSessionTelemetry.disabled()
    tel.record("gdbstub", "ok", 12.0)
    assert not _points(reader, "kdive.debug.session.duration"), "disabled() must not emit"


def test_negative_seconds_are_not_recorded() -> None:
    """A negative duration (app/DB clock skew) must not pollute the histogram."""
    reader = InMemoryMetricReader()
    tel = DebugSessionTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tel.record("gdbstub", "ok", -0.001)
    assert not _points(reader, "kdive.debug.session.duration"), "negative duration must not emit"


def test_zero_and_subsecond_durations_are_recorded() -> None:
    """The drop guard rejects only negative durations: a 0s or sub-1s session still counts."""
    reader = InMemoryMetricReader()
    tel = DebugSessionTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tel.record("gdbstub", "ok", 0.0)
    tel.record("drgn-live", "error", 0.5)
    pts = _points(reader, "kdive.debug.session.duration")
    assert pts, "zero / sub-second durations must be recorded, not dropped"
    assert sum(p.count for p in pts) == 2
