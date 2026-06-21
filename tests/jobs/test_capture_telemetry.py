"""Tests for CaptureTelemetry (ADR-0191 H1)."""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.jobs.handlers.capture_telemetry import CaptureTelemetry


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
    raise AssertionError(f"metric {name!r} not exported")


def test_record_success_emits_duration_and_bytes() -> None:
    reader = InMemoryMetricReader()
    tel = CaptureTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tel.record("host_dump", "local-libvirt", "ok", seconds=5.0, size_bytes=1024)
    dur_pts = _points(reader, "kdive.vmcore.capture.duration")
    byte_pts = _points(reader, "kdive.vmcore.capture.bytes")
    assert dur_pts, "duration not emitted on success"
    assert dur_pts[0].attributes["capture_method"] == "host_dump"
    assert dur_pts[0].attributes["provider"] == "local-libvirt"
    assert dur_pts[0].attributes["outcome"] == "ok"
    assert byte_pts, "bytes not emitted on success"
    assert byte_pts[0].attributes["capture_method"] == "host_dump"
    assert byte_pts[0].attributes["provider"] == "local-libvirt"


def test_record_error_emits_duration_but_no_bytes() -> None:
    reader = InMemoryMetricReader()
    tel = CaptureTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tel.record("kdump", "remote-libvirt", "error", seconds=2.0)
    dur_pts = _points(reader, "kdive.vmcore.capture.duration")
    byte_pts = _points(reader, "kdive.vmcore.capture.bytes")
    assert dur_pts, "duration not emitted on error"
    assert dur_pts[0].attributes["outcome"] == "error"
    assert not byte_pts, "bytes must not be emitted on error"


def test_histograms_declare_their_schema_and_bucket_boundaries() -> None:
    reader = InMemoryMetricReader()
    tel = CaptureTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tel.record("host_dump", "local-libvirt", "ok", seconds=5.0, size_bytes=1024)

    duration = _metric(reader, "kdive.vmcore.capture.duration")
    assert duration.unit == "s"
    assert duration.description == "vmcore capture wall-clock duration, by method and provider."
    assert duration.data.data_points[0].explicit_bounds == (
        1.0,
        5.0,
        15.0,
        60.0,
        300.0,
        900.0,
        1800.0,
    )

    size = _metric(reader, "kdive.vmcore.capture.bytes")
    assert size.unit == "By"
    assert size.description == "Raw vmcore size captured, by method and provider."
    assert size.data.data_points[0].explicit_bounds == (
        1e6,
        1e7,
        1e8,
        5e8,
        1e9,
        5e9,
    )


def test_disabled_is_noop() -> None:
    reader = InMemoryMetricReader()
    tel = CaptureTelemetry.disabled()
    tel.record("host_dump", "local-libvirt", "ok", seconds=1.0, size_bytes=512)
    assert not _points(reader, "kdive.vmcore.capture.duration")
    assert not _points(reader, "kdive.vmcore.capture.bytes")
