"""Reconciler per-pass telemetry + loop-granularity heartbeat (ADR-0090 §5).

The reconciler ticks the ``/livez`` heartbeat once per pass (not per repair) and emits a
per-pass span + duration/lag metrics. These run without a DB by stubbing ``run_once``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import cast

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from kdive.health.heartbeat import Heartbeat
from kdive.providers.infra.reaping import NullReaper
from kdive.reconciler import loop as reconciler_loop
from kdive.reconciler.loop import ReconcileConfig, Reconciler, ReconcileReport
from kdive.reconciler.loop_telemetry import ReconcilerTelemetry


def _empty_report() -> ReconcileReport:
    return ReconcileReport(
        expired_allocations=0,
        orphaned_systems=0,
        abandoned_jobs=0,
        dead_sessions=0,
        leaked_domains=0,
        idempotency_keys_gc_count=0,
        failures=(),
    )


class _CountingHeartbeat:
    def __init__(self) -> None:
        self.ticks = 0

    def tick(self) -> None:
        self.ticks += 1


def _telemetry() -> tuple[ReconcilerTelemetry, InMemoryMetricReader, InMemorySpanExporter]:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return ReconcilerTelemetry(tracer=tp.get_tracer("test"), meter=meter), reader, exporter


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    assert data is not None
    names: set[str] = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            names.update(m.name for m in sm.metrics)
    return names


def _counter_points(
    reader: InMemoryMetricReader, name: str
) -> dict[tuple[tuple[str, str], ...], float]:
    """Return {sorted-attr-tuple: value} for the number-data points of metric ``name``."""
    data = reader.get_metrics_data()
    assert data is not None
    points: dict[tuple[tuple[str, str], ...], float] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    value = getattr(point, "value", None)  # NumberDataPoint only
                    if value is None:
                        continue
                    attrs = point.attributes or {}
                    key = tuple(sorted((str(k), str(v)) for k, v in attrs.items()))
                    points[key] = value
    return points


def _hist_points(reader: InMemoryMetricReader, name: str) -> dict[tuple[tuple[str, str], ...], int]:
    """Return {sorted-attr-tuple: data-point count} for histogram metric ``name``."""
    data = reader.get_metrics_data()
    points: dict[tuple[tuple[str, str], ...], int] = {}
    if data is None:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    count = getattr(point, "count", None)  # HistogramDataPoint only
                    if count is None:
                        continue
                    attrs = point.attributes or {}
                    key = tuple(sorted((str(k), str(v)) for k, v in attrs.items()))
                    points[key] = count
    return points


def _metric_meta(reader: InMemoryMetricReader, name: str) -> tuple[str, str]:
    """Return ``(unit, description)`` for metric ``name`` (asserts it exists)."""
    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    return metric.unit, metric.description
    raise AssertionError(f"metric {name!r} not found")


def test_instrument_names_and_units() -> None:
    """The four instruments use their documented names + units (ADR-0090 §5)."""
    telemetry, reader, _ = _telemetry()
    # Touch every instrument so each series is exported.
    with telemetry.pass_span() as span:
        span.set_outcome("ok")
    telemetry.observe_lag(0.0)
    telemetry.record_repairs({"orphaned_systems": 0}, failures=["orphaned_systems"])

    names = _metric_names(reader)
    assert {
        "kdive.reconcile.duration",
        "kdive.reconcile.lag",
        "kdive.reconciler.repairs",
        "kdive.errors",
    } <= names

    assert _metric_meta(reader, "kdive.reconcile.duration")[0] == "s"
    assert _metric_meta(reader, "kdive.reconcile.lag")[0] == "s"
    assert _metric_meta(reader, "kdive.reconciler.repairs")[0] == "1"
    assert _metric_meta(reader, "kdive.errors")[0] == "1"


def test_observe_lag_records_zero_gap() -> None:
    """A lag of exactly 0.0 is a valid on-time pass and must be recorded (>= 0.0)."""
    telemetry, reader, _ = _telemetry()
    telemetry.observe_lag(0.0)
    points = _hist_points(reader, "kdive.reconcile.lag")
    assert points == {(): 1}


def test_observe_lag_drops_negative_gap() -> None:
    """A negative lag is nonsensical (clock skew) and must be dropped, not recorded."""
    telemetry, reader, _ = _telemetry()
    telemetry.observe_lag(-1.0)
    assert _hist_points(reader, "kdive.reconcile.lag") == {}


def test_pass_span_ok_outcome_labels_duration_and_span() -> None:
    """An ok pass stamps the span outcome=ok, leaves status unset, labels duration ok."""
    telemetry, reader, exporter = _telemetry()
    with telemetry.pass_span() as span:
        span.set_outcome("ok")

    finished = exporter.get_finished_spans()
    assert finished[0].attributes is not None
    assert finished[0].attributes["outcome"] == "ok"
    assert finished[0].status.status_code is not StatusCode.ERROR

    points = _hist_points(reader, "kdive.reconcile.duration")
    assert points == {(("outcome", "ok"),): 1}


def test_pass_span_default_outcome_is_ok() -> None:
    """Without set_outcome the span defaults to outcome=ok on both span and duration."""
    telemetry, reader, exporter = _telemetry()
    with telemetry.pass_span():
        pass
    assert exporter.get_finished_spans()[0].attributes["outcome"] == "ok"
    assert _hist_points(reader, "kdive.reconcile.duration") == {(("outcome", "ok"),): 1}


def test_pass_span_error_outcome_sets_error_status() -> None:
    """An error outcome stamps outcome=error, sets ERROR status, labels duration error."""
    telemetry, reader, exporter = _telemetry()
    with telemetry.pass_span() as span:
        span.set_outcome("error")

    finished = exporter.get_finished_spans()
    assert finished[0].attributes["outcome"] == "error"
    assert finished[0].status.status_code is StatusCode.ERROR

    points = _hist_points(reader, "kdive.reconcile.duration")
    assert points == {(("outcome", "error"),): 1}


def test_pass_span_is_internal_kind() -> None:
    """The per-pass span is an INTERNAL span (ADR-0090 §5)."""
    telemetry, _, exporter = _telemetry()
    with telemetry.pass_span():
        pass
    assert exporter.get_finished_spans()[0].kind is SpanKind.INTERNAL


def test_record_repairs_emits_per_kind_counts() -> None:
    telemetry, reader, _ = _telemetry()
    telemetry.record_repairs(
        {"orphaned_systems": 2, "promoted_allocations": 0, "leaked_domains": 1}, failures=[]
    )
    points = _counter_points(reader, "kdive.reconciler.repairs")
    assert points[(("repair_kind", "orphaned_systems"),)] == 2
    assert points[(("repair_kind", "leaked_domains"),)] == 1
    # A zero-count kind still emits its series so the metric is present from the start.
    assert points[(("repair_kind", "promoted_allocations"),)] == 0


def test_record_repairs_failure_increments_errors() -> None:
    telemetry, reader, _ = _telemetry()
    telemetry.record_repairs({"leaked_domains": 0}, failures=["leaked_domains", "orphaned_systems"])
    points = _counter_points(reader, "kdive.errors")
    assert points[(("error_category", "infrastructure_failure"),)] == 2


def test_record_repairs_disabled_is_noop() -> None:
    telemetry = ReconcilerTelemetry.disabled()
    # No meter wired; must be a silent no-op rather than raising.
    telemetry.record_repairs({"orphaned_systems": 1}, failures=["orphaned_systems"])


def test_disabled_pass_span_is_noop() -> None:
    telemetry = ReconcilerTelemetry.disabled()
    with telemetry.pass_span() as span:
        span.set_outcome("ok")
    telemetry.observe_lag(1.0)  # no meter; must not raise


def test_pass_span_records_duration_and_emits_span() -> None:
    telemetry, reader, exporter = _telemetry()
    with telemetry.pass_span() as span:
        span.set_outcome("ok")
    assert "kdive.reconcile.duration" in _metric_names(reader)
    spans = exporter.get_finished_spans()
    assert spans and spans[0].name == "reconcile/pass"


def test_background_ticker_keeps_livez_live_across_a_long_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that blocks far past stale_after must NOT flip /livez stale (ADR-0090 §5)."""

    async def _run() -> None:
        hb = Heartbeat(stale_after=0.05)
        reconciler = Reconciler(
            pool=_FakePool(),  # ty: ignore[invalid-argument-type]
            reaper=NullReaper(),
            config=ReconcileConfig(
                interval=timedelta(milliseconds=1),
                heartbeat=hb,
                heartbeat_tick=timedelta(milliseconds=5),
            ),
        )
        stop = asyncio.Event()
        live_during_pass: list[bool] = []

        async def long_run_once() -> ReconcileReport:
            await asyncio.sleep(0.2)  # a slow pass far longer than stale_after
            live_during_pass.append(hb.is_live())
            stop.set()
            return _empty_report()

        monkeypatch.setattr(reconciler, "run_once", long_run_once)
        await asyncio.wait_for(reconciler.run(stop), timeout=2)
        assert live_during_pass == [True]

    asyncio.run(_run())


def test_background_ticker_does_not_tick_after_stop() -> None:
    async def _run() -> None:
        heartbeat = _CountingHeartbeat()
        stop = asyncio.Event()
        task = asyncio.create_task(
            reconciler_loop._tick_until_stop(
                cast(Heartbeat, heartbeat),
                stop,
                60.0,
            )
        )
        await asyncio.sleep(0)
        assert heartbeat.ticks == 1

        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert heartbeat.ticks == 1

    asyncio.run(_run())


class _FakePool:
    """A stand-in pool; the heartbeat test stubs run_once so the pool is never used."""
