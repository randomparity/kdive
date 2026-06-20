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
