"""Tests for the finalized console-bytes counter (ADR-0191 H2)."""

from __future__ import annotations

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.reconciler.console_telemetry import ConsoleTelemetry


def _points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    if data is None:
        return []
    out = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


def test_record_nonzero_adds_under_success() -> None:
    reader = InMemoryMetricReader()
    telem = ConsoleTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    telem.record(5)
    pts = _points(reader, "kdive.console.bytes")
    assert len(pts) == 1
    assert pts[0].attributes["outcome"] == "success"
    assert pts[0].value == 5


def test_record_zero_adds_under_empty() -> None:
    reader = InMemoryMetricReader()
    telem = ConsoleTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    telem.record(0)
    pts = _points(reader, "kdive.console.bytes")
    assert len(pts) == 1
    assert pts[0].attributes["outcome"] == "empty"
    assert pts[0].value == 0


def test_no_system_identifier_on_points() -> None:
    reader = InMemoryMetricReader()
    telem = ConsoleTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    telem.record(42)
    pts = _points(reader, "kdive.console.bytes")
    assert pts
    attrs = dict(pts[0].attributes)
    assert "system" not in attrs
    assert "system_id" not in attrs


def test_disabled_is_noop() -> None:
    reader = InMemoryMetricReader()
    # Register the reader with a provider so get_metrics_data() works; the disabled
    # telemetry object uses no meter of its own and emits nothing.
    MeterProvider(metric_readers=[reader])
    telem = ConsoleTelemetry.disabled()
    telem.record(100)
    pts = _points(reader, "kdive.console.bytes")
    assert not pts
