"""Per-job worker telemetry: span + duration/queue-depth metrics (ADR-0090 §5).

Drives :class:`WorkerTelemetry` against a real in-memory meter/tracer and asserts the
emitted instruments carry only allowlisted labels (``job_kind``/``outcome``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.worker_telemetry import WorkerTelemetry

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job(state: JobState, category: ErrorCategory | None = None) -> Job:
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.BUILD,
        payload={},
        state=state,
        max_attempts=3,
        error_category=category,
        authorizing={"principal": "alice", "agent_session": None, "project": "proj"},
        dedup_key=str(uuid4()),
    )


def _telemetry() -> tuple[WorkerTelemetry, InMemoryMetricReader, InMemorySpanExporter]:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return WorkerTelemetry(tracer=tp.get_tracer("test"), meter=meter), reader, exporter


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    assert data is not None
    names: set[str] = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            names.update(m.name for m in sm.metrics)
    return names


def test_disabled_is_a_noop() -> None:
    telemetry = WorkerTelemetry.disabled()
    with telemetry.job_span("build") as span:
        span.set_outcome("ok")
    telemetry.observe_queue_depth(3)  # no meter wired; must not raise


def test_job_span_records_duration_and_labels() -> None:
    telemetry, reader, exporter = _telemetry()
    with telemetry.job_span("build") as span:
        span.set_outcome("ok")
    assert "kdive.job.duration" in _metric_names(reader)
    spans = exporter.get_finished_spans()
    assert spans and spans[0].name == "job/build"
    assert spans[0].attributes is not None
    assert spans[0].attributes["job_kind"] == "build"
    assert spans[0].attributes["outcome"] == "ok"


def test_job_span_error_sets_error_status() -> None:
    telemetry, _reader, exporter = _telemetry()
    with telemetry.job_span("teardown") as span:
        span.set_outcome("error")
    spans = exporter.get_finished_spans()
    assert spans[0].attributes is not None
    assert spans[0].attributes["outcome"] == "error"
    assert spans[0].status.status_code.name == "ERROR"


def _gauge_value(reader: InMemoryMetricReader, name: str) -> float | int | None:
    from opentelemetry.sdk.metrics.export import NumberDataPoint

    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    points = list(metric.data.data_points)
                    last = points[-1] if points else None
                    assert last is None or isinstance(last, NumberDataPoint)
                    return last.value if last is not None else None
    return None


def test_queue_depth_gauge_reports_last_observed_not_a_running_sum() -> None:
    telemetry, reader, _exporter = _telemetry()
    telemetry.observe_queue_depth(3)
    assert _gauge_value(reader, "kdive.job.queue.depth") == 3
    # A second observation REPLACES, not accumulates — it is a gauge, not a counter.
    telemetry.observe_queue_depth(1)
    assert _gauge_value(reader, "kdive.job.queue.depth") == 1


def test_queue_depth_disabled_is_noop() -> None:
    WorkerTelemetry.disabled().observe_queue_depth(5)  # must not raise


def _error_points(reader: InMemoryMetricReader) -> dict[tuple[tuple[str, str], ...], float]:
    data = reader.get_metrics_data()
    assert data is not None
    points: dict[tuple[tuple[str, str], ...], float] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != "kdive.errors":
                    continue
                for point in metric.data.data_points:
                    attrs = point.attributes or {}
                    key = tuple(sorted((str(k), str(v)) for k, v in attrs.items()))
                    points[key] = getattr(point, "value", 0)
    return points


def test_record_job_failure_increments_errors_by_category() -> None:
    telemetry, reader, _ = _telemetry()
    job = _job(JobState.FAILED, ErrorCategory.BUILD_FAILURE)
    telemetry.record_job_failure(job, ErrorCategory.BUILD_FAILURE)
    points = _error_points(reader)
    assert points[(("error_category", "build_failure"),)] == 1


def test_record_job_failure_skips_a_requeued_job() -> None:
    telemetry, reader, _ = _telemetry()
    telemetry.record_job_failure(_job(JobState.QUEUED), ErrorCategory.TRANSPORT_FAILURE)
    # A non-terminal (requeued) job is a retry, not a failure origin → not counted.
    assert _error_points(reader) == {}


def test_record_job_failure_disabled_is_noop() -> None:
    WorkerTelemetry.disabled().record_job_failure(
        _job(JobState.FAILED, ErrorCategory.BUILD_FAILURE), ErrorCategory.BUILD_FAILURE
    )
