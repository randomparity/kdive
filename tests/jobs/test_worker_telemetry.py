"""Per-job worker telemetry: span + duration/queue-depth metrics (ADR-0090 §5).

Drives :class:`WorkerTelemetry` against a real in-memory meter/tracer and asserts the
emitted instruments carry only allowlisted labels (``job_kind``/``outcome``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
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


def _instruments(reader: InMemoryMetricReader) -> dict[str, tuple[str, str]]:
    """Map each emitted instrument name to its ``(unit, description)`` contract."""
    data = reader.get_metrics_data()
    assert data is not None
    out: dict[str, tuple[str, str]] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = (metric.unit, metric.description)
    return out


def test_instrument_contract_names_units_and_descriptions() -> None:
    # The exported (name, unit, description) of every instrument is a stable contract:
    # dashboards and alerts query by name and render units, so a drifted unit or description
    # is a real regression. Drive every instrument once so each surfaces in the reader.
    telemetry, reader, _exporter = _telemetry()
    from kdive.jobs.provider_context import set_provider_kind

    telemetry.observe_queue_depth(1)
    with telemetry.job_span("build") as span:
        set_provider_kind("local-libvirt")
        span.set_outcome("error")  # also drives provider.op.errors
    telemetry.record_job_failure(
        _job(JobState.FAILED, ErrorCategory.BUILD_FAILURE), ErrorCategory.BUILD_FAILURE
    )
    telemetry.record_time_to_claim("build", 1.0)
    telemetry.record_job_retry("build")

    assert _instruments(reader) == {
        "kdive.job.duration": ("s", "Worker job-handler wall-clock duration."),
        "kdive.job.queue.depth": ("1", "Claimable jobs in the queue at the last poll."),
        "kdive.errors": (
            "1",
            "Categorized failures at their backend origin, by error category.",
        ),
        "kdive.provider.op.duration": (
            "s",
            "Provider-operation wall-clock duration, by provider and job kind.",
        ),
        "kdive.provider.op.errors": ("1", "Failed provider operations, by provider and job kind."),
        "kdive.job.time_to_claim": ("s", "Queue latency from enqueue to claim, by job kind."),
        "kdive.job.retries": ("1", "Job requeues (non-terminal failures), by job kind."),
    }


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
    points = _points_for(reader, "kdive.job.duration")
    assert len(points) == 1
    # The duration point carries exactly the allowlisted labels under their exact keys.
    assert dict(points[0].attributes) == {"job_kind": "build", "outcome": "ok"}
    spans = exporter.get_finished_spans()
    assert spans and spans[0].name == "job/build"
    assert spans[0].attributes is not None
    assert spans[0].attributes["job_kind"] == "build"
    assert spans[0].attributes["outcome"] == "ok"


def test_job_span_default_outcome_is_ok_when_unset() -> None:
    # A span whose handler never calls set_outcome records the default "ok" outcome label.
    telemetry, reader, _exporter = _telemetry()
    with telemetry.job_span("build"):
        pass
    points = _points_for(reader, "kdive.job.duration")
    assert len(points) == 1
    assert points[0].attributes["outcome"] == "ok"


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


def test_queue_depth_gauge_starts_at_zero_before_any_observation() -> None:
    # A freshly-built telemetry reports an empty backlog (0), not a phantom one, until the
    # first poll observes a real depth.
    telemetry, reader, _exporter = _telemetry()
    assert _gauge_value(reader, "kdive.job.queue.depth") == 0


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


def _points_for(reader: InMemoryMetricReader, family_name: str) -> list[Any]:
    """Return all data points whose metric name matches ``family_name``."""
    data = reader.get_metrics_data()
    assert data is not None
    out: list[Any] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == family_name:
                    out.extend(metric.data.data_points)
    return out


def test_provider_op_duration_recorded_when_kind_tagged() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    tracer = TracerProvider().get_tracer("test")
    telem = WorkerTelemetry(tracer=tracer, meter=meter)
    from kdive.jobs.provider_context import set_provider_kind

    with telem.job_span("build") as span:
        set_provider_kind("local-libvirt")
        span.set_outcome("ok")
    points = _points_for(reader, "kdive.provider.op.duration")
    assert points, "provider-op duration not emitted for a tagged job"
    assert points[0].attributes["provider"] == "local-libvirt"
    assert points[0].attributes["job_kind"] == "build"
    # An ok provider op carries the outcome label and emits no provider-op error point.
    assert points[0].attributes["outcome"] == "ok"
    assert _points_for(reader, "kdive.provider.op.errors") == []


def test_provider_op_error_increments_error_counter_and_tags_outcome() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    tracer = TracerProvider().get_tracer("test")
    telem = WorkerTelemetry(tracer=tracer, meter=meter)
    from kdive.jobs.provider_context import set_provider_kind

    with telem.job_span("build") as span:
        set_provider_kind("local-libvirt")
        span.set_outcome("error")

    duration = _points_for(reader, "kdive.provider.op.duration")
    assert duration and duration[0].attributes["outcome"] == "error"
    errors = _points_for(reader, "kdive.provider.op.errors")
    assert len(errors) == 1
    assert errors[0].value == 1
    assert errors[0].attributes["provider"] == "local-libvirt"
    assert errors[0].attributes["job_kind"] == "build"


def test_provider_op_not_recorded_for_untagged_job_and_no_leak() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    tracer = TracerProvider().get_tracer("test")
    telem = WorkerTelemetry(tracer=tracer, meter=meter)
    from kdive.jobs.provider_context import set_provider_kind

    with telem.job_span("build") as span:  # tagged
        set_provider_kind("remote-libvirt")
        span.set_outcome("ok")
    with telem.job_span("teardown"):  # untagged — must NOT inherit remote-libvirt
        pass
    points = _points_for(reader, "kdive.provider.op.duration")
    kinds = {p.attributes["job_kind"] for p in points}
    assert "teardown" not in kinds


# --- Group I: time-to-claim + retries (ADR-0191 I) ---


def test_record_time_to_claim_emits_one_point_with_job_kind() -> None:
    telemetry, reader, _ = _telemetry()
    telemetry.record_time_to_claim("build", 3.0)
    points = _points_for(reader, "kdive.job.time_to_claim")
    assert len(points) == 1
    assert points[0].attributes["job_kind"] == "build"


def test_record_time_to_claim_negative_seconds_is_noop() -> None:
    telemetry, reader, _ = _telemetry()
    telemetry.record_time_to_claim("build", -1.0)
    assert _points_for(reader, "kdive.job.time_to_claim") == []


def test_record_time_to_claim_zero_seconds_is_recorded() -> None:
    # A job claimed the instant it was enqueued has zero latency: 0.0 is a valid sample
    # (the guard is ``>= 0.0``), not a discarded blip.
    telemetry, reader, _ = _telemetry()
    telemetry.record_time_to_claim("build", 0.0)
    points = _points_for(reader, "kdive.job.time_to_claim")
    assert len(points) == 1
    assert points[0].count == 1


def test_record_time_to_claim_subsecond_latency_is_recorded() -> None:
    # Sub-second queue latency must be recorded — the lower bound is zero, not one second.
    telemetry, reader, _ = _telemetry()
    telemetry.record_time_to_claim("build", 0.5)
    points = _points_for(reader, "kdive.job.time_to_claim")
    assert len(points) == 1
    assert points[0].count == 1


def test_record_time_to_claim_disabled_is_noop() -> None:
    WorkerTelemetry.disabled().record_time_to_claim("build", 3.0)  # must not raise


def test_record_job_retry_increments_counter_with_job_kind() -> None:
    telemetry, reader, _ = _telemetry()
    telemetry.record_job_retry("build")
    points = _points_for(reader, "kdive.job.retries")
    assert len(points) == 1
    assert points[0].attributes["job_kind"] == "build"
    assert points[0].value == 1


def test_record_job_retry_disabled_is_noop() -> None:
    WorkerTelemetry.disabled().record_job_retry("build")  # must not raise
