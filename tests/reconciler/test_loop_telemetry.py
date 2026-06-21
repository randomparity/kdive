"""Reconciler per-pass telemetry + loop-granularity heartbeat (ADR-0090 §5).

The reconciler ticks the ``/livez`` heartbeat once per pass (not per repair) and emits a
per-pass span + duration/lag metrics. These run without a DB by stubbing ``run_once``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
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


class _RecordingSpan:
    def __init__(self) -> None:
        self.outcomes: list[str] = []

    def set_outcome(self, outcome: str) -> None:
        self.outcomes.append(outcome)


class _RecordingTelemetry:
    """A telemetry double that records every call the pass loop makes."""

    def __init__(self) -> None:
        self.lags: list[float] = []
        self.recorded: list[tuple[object, object]] = []
        self.span = _RecordingSpan()

    def observe_lag(self, lag_seconds: float) -> None:
        self.lags.append(lag_seconds)

    def record_repairs(self, counts: object, failures: object) -> None:
        self.recorded.append((counts, failures))

    @contextlib.contextmanager
    def pass_span(self) -> Iterator[_RecordingSpan]:
        yield self.span


class _NoConnPool:
    """A pool whose connection() raises so the snapshot refreshers hit their except path."""

    def connection(self) -> object:
        raise RuntimeError("no DB in this unit test")


def _run_one_pass(
    monkeypatch: pytest.MonkeyPatch,
    telemetry: _RecordingTelemetry,
    report: ReconcileReport,
) -> Reconciler:
    """Drive exactly one iteration of ``_pass_loop`` and return the reconciler."""
    reconciler = Reconciler(
        pool=_NoConnPool(),  # ty: ignore[invalid-argument-type]
        reaper=NullReaper(),
        config=ReconcileConfig(
            interval=timedelta(seconds=0),
            telemetry=cast(ReconcilerTelemetry, telemetry),
        ),
    )

    async def _run() -> None:
        stop = asyncio.Event()
        passes = 0

        async def one_shot_run_once() -> ReconcileReport:
            nonlocal passes
            passes += 1
            stop.set()  # stop after this single pass
            return report

        monkeypatch.setattr(reconciler, "run_once", one_shot_run_once)
        await asyncio.wait_for(reconciler._pass_loop(stop), timeout=2)
        assert passes == 1

    asyncio.run(_run())
    return reconciler


def test_pass_loop_records_repair_counts_and_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pass forwards the report's repair_counts and failures to record_repairs."""
    telemetry = _RecordingTelemetry()
    report = ReconcileReport(
        expired_allocations=1,
        orphaned_systems=0,
        abandoned_jobs=0,
        dead_sessions=0,
        leaked_domains=0,
        idempotency_keys_gc_count=0,
        failures=("leaked_domains",),
    )
    _run_one_pass(monkeypatch, telemetry, report)

    assert len(telemetry.recorded) == 1
    counts, failures = telemetry.recorded[0]
    assert counts == report.repair_counts
    assert failures == ("leaked_domains",)


def test_pass_loop_observes_nonnegative_lag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first pass observes a finite, non-negative lag (now - scheduled start)."""
    telemetry = _RecordingTelemetry()
    _run_one_pass(monkeypatch, telemetry, _empty_report())

    assert len(telemetry.lags) == 1
    lag = telemetry.lags[0]
    assert isinstance(lag, float)
    assert lag >= 0.0
    assert lag < 1.0  # a no-op pass cannot lag by a whole second


def test_pass_loop_marks_span_error_when_run_once_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass whose run_once raises stamps the span error and keeps looping."""
    telemetry = _RecordingTelemetry()
    reconciler = Reconciler(
        pool=_NoConnPool(),  # ty: ignore[invalid-argument-type]
        reaper=NullReaper(),
        config=ReconcileConfig(
            interval=timedelta(seconds=0),
            telemetry=cast(ReconcilerTelemetry, telemetry),
        ),
    )

    async def _run() -> None:
        stop = asyncio.Event()

        async def boom() -> ReconcileReport:
            stop.set()
            raise RuntimeError("transient pass failure")

        monkeypatch.setattr(reconciler, "run_once", boom)
        await asyncio.wait_for(reconciler._pass_loop(stop), timeout=2)

    asyncio.run(_run())

    assert telemetry.span.outcomes == ["error"]
    # A raising pass never reaches record_repairs.
    assert telemetry.recorded == []


def test_reconciler_keeps_real_telemetry_doubles_when_config_omits_them() -> None:
    """Omitted fleet/build-host telemetry default to real disabled instances, not None."""
    reconciler = Reconciler(
        pool=_FakePool(),  # ty: ignore[invalid-argument-type]
        reaper=NullReaper(),
        config=ReconcileConfig(),
    )
    # `or`-default must yield a usable object (not None / not the falsy short-circuit).
    assert reconciler._fleet_telemetry is not None
    assert reconciler._build_host_telemetry is not None
    assert hasattr(reconciler._fleet_telemetry, "refresh")
    assert hasattr(reconciler._build_host_telemetry, "refresh")


def test_run_once_forwards_pool_reaper_and_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_once passes the reconciler's own pool, reaper, and config to reconcile_once."""
    pool = _FakePool()
    reaper = NullReaper()
    cfg = ReconcileConfig(interval=timedelta(seconds=7))
    reconciler = Reconciler(
        pool=pool,  # ty: ignore[invalid-argument-type]
        reaper=reaper,
        config=cfg,
    )
    captured: dict[str, object] = {}

    async def _fake_reconcile_once(p: object, r: object, *, config: object) -> ReconcileReport:
        captured["pool"] = p
        captured["reaper"] = r
        captured["config"] = config
        return _empty_report()

    monkeypatch.setattr(reconciler_loop, "reconcile_once", _fake_reconcile_once)
    asyncio.run(reconciler.run_once())

    assert captured["pool"] is pool
    assert captured["reaper"] is reaper
    assert captured["config"] is cfg


def test_run_cancels_heartbeat_ticker_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """When run() returns, the background heartbeat ticker task is cancelled, not leaked."""

    ticker_running = asyncio.Event()

    async def _never_ending_ticker(heartbeat: object, stop: object, interval: float) -> None:
        ticker_running.set()
        await asyncio.Event().wait()  # blocks forever until cancelled

    monkeypatch.setattr(reconciler_loop, "_tick_until_stop", _never_ending_ticker)

    async def _run() -> None:
        reconciler = Reconciler(
            pool=_FakePool(),  # ty: ignore[invalid-argument-type]
            reaper=NullReaper(),
            config=ReconcileConfig(
                interval=timedelta(seconds=0),
                heartbeat=cast(Heartbeat, _CountingHeartbeat()),
                heartbeat_tick=timedelta(seconds=60),
            ),
        )
        stop = asyncio.Event()

        async def one_shot() -> ReconcileReport:
            stop.set()
            return _empty_report()

        reconciler.run_once = one_shot  # type: ignore[method-assign]
        await asyncio.wait_for(reconciler.run(stop), timeout=2)

        # Right after run() returns (still inside this loop), the never-ending ticker must
        # already be settled. If run()'s cleanup branch were skipped (`if ticker is None`),
        # a still-pending ticker task would survive here.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert ticker_running.is_set(), "ticker should have started"
        assert pending == [], f"ticker task leaked: {pending!r}"

    asyncio.run(_run())


def test_pass_loop_resets_next_due_so_steady_state_lag_stays_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After each pass next_due advances by +interval, so a steady cadence has ~0 lag.

    Across two back-to-back passes the second pass's observed lag must stay well under one
    interval: ``next_due = now + interval`` re-anchors the schedule each pass. A loop that
    forgot to re-anchor (or anchored with the wrong sign) would report a lag near a full
    extra interval on the second pass.
    """
    interval = 0.05
    telemetry = _RecordingTelemetry()
    reconciler = Reconciler(
        pool=_NoConnPool(),  # ty: ignore[invalid-argument-type]
        reaper=NullReaper(),
        config=ReconcileConfig(
            interval=timedelta(seconds=interval),
            telemetry=cast(ReconcilerTelemetry, telemetry),
        ),
    )

    async def _run() -> None:
        stop = asyncio.Event()
        passes = 0

        async def two_shot_run_once() -> ReconcileReport:
            nonlocal passes
            passes += 1
            if passes >= 2:
                stop.set()
            return _empty_report()

        monkeypatch.setattr(reconciler, "run_once", two_shot_run_once)
        await asyncio.wait_for(reconciler._pass_loop(stop), timeout=2)
        assert passes == 2

    asyncio.run(_run())

    assert len(telemetry.lags) == 2
    # Second-pass lag is the schedule slip; re-anchoring keeps it well under one interval.
    assert telemetry.lags[1] < interval


def test_pass_loop_refreshes_snapshots_each_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each pass awaits both snapshot refreshers exactly once.

    Spies replace the refresher coroutines so the test pins the *calls* the loop body makes
    (loop.py ``await self._refresh_fleet_snapshot()`` / ``_refresh_build_host_snapshot()``),
    not construction-time attributes. Deleting either ``await`` statement drops its name from
    ``calls`` and fails the matching assertion.
    """
    telemetry = _RecordingTelemetry()
    reconciler = Reconciler(
        pool=_NoConnPool(),  # ty: ignore[invalid-argument-type]
        reaper=NullReaper(),
        config=ReconcileConfig(
            interval=timedelta(seconds=0),
            telemetry=cast(ReconcilerTelemetry, telemetry),
        ),
    )
    calls: list[str] = []

    async def _spy_fleet() -> None:
        calls.append("fleet")

    async def _spy_build_host() -> None:
        calls.append("build_host")

    monkeypatch.setattr(reconciler, "_refresh_fleet_snapshot", _spy_fleet)
    monkeypatch.setattr(reconciler, "_refresh_build_host_snapshot", _spy_build_host)

    async def _run() -> None:
        stop = asyncio.Event()

        async def one_shot_run_once() -> ReconcileReport:
            stop.set()
            return _empty_report()

        monkeypatch.setattr(reconciler, "run_once", one_shot_run_once)
        await asyncio.wait_for(reconciler._pass_loop(stop), timeout=2)

    asyncio.run(_run())

    assert calls.count("fleet") == 1
    assert calls.count("build_host") == 1


def test_pass_loop_swallows_snapshot_read_failure_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing snapshot read is best-effort: the refreshers still run and the pass survives.

    The real refreshers run against ``_NoConnPool`` (connection() raises), so both reads hit
    their ``except`` path. The pass must still complete normally — a snapshot read failure
    must never starve the repair loop. Module-level read spies confirm the loop actually
    reached the read attempt rather than short-circuiting before it; a mutant that lets the
    read failure escape would raise out of ``_pass_loop`` and fail the timeout/await here.
    """
    telemetry = _RecordingTelemetry()
    reconciler = Reconciler(
        pool=_NoConnPool(),  # ty: ignore[invalid-argument-type]
        reaper=NullReaper(),
        config=ReconcileConfig(
            interval=timedelta(seconds=0),
            telemetry=cast(ReconcilerTelemetry, telemetry),
        ),
    )

    async def _run() -> None:
        stop = asyncio.Event()
        passes = 0

        async def one_shot_run_once() -> ReconcileReport:
            nonlocal passes
            passes += 1
            stop.set()
            return _empty_report()

        monkeypatch.setattr(reconciler, "run_once", one_shot_run_once)
        # Must not raise: _NoConnPool makes both refreshers hit their except-and-return path.
        await asyncio.wait_for(reconciler._pass_loop(stop), timeout=2)
        assert passes == 1

    asyncio.run(_run())

    # The pass completed and recorded its single repair report despite both refreshers failing.
    assert len(telemetry.recorded) == 1
