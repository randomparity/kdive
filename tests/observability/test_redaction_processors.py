"""Three-signal redaction at the OTel SDK boundary (ADR-0090 §4).

A registered secret placed in a log body, a span attribute, and a metric label must
be scrubbed in every exporter's output — the failure mode the single dedicated test
guards against is "logs are clean so we assumed traces were too."
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kdive.observability import redaction as orx
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry

_SECRET = "sk-super-secret-value"  # pragma: allowlist secret - test fixture value


def _registry() -> SecretRegistry:
    registry = SecretRegistry()
    registry.register(_SECRET, scope=None)
    return registry


def test_secret_in_log_body_is_redacted_before_export() -> None:
    registry = _registry()
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(orx.RedactingLogProcessor(registry))
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(logger_provider=provider)
    logger = logging.getLogger("kdive.test.redact.log")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    logger.info("connecting with %s now", _SECRET)
    provider.force_flush()

    bodies = [str(r.log_record.body) for r in exporter.get_finished_logs()]
    assert bodies, "expected an exported log record"
    assert _SECRET not in "".join(bodies)
    assert REDACTION in "".join(bodies)


def test_secret_in_span_attribute_is_redacted_before_export() -> None:
    registry = _registry()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(orx.RedactingSpanExporter(exporter, registry)))
    tracer = provider.get_tracer("kdive.test.redact.span")

    with tracer.start_as_current_span("op") as span:
        span.set_attribute("connection_url", f"grpc://user:{_SECRET}@host:4317")

    spans = exporter.get_finished_spans()
    assert spans, "expected an exported span"
    rendered = str(dict(spans[0].attributes or {}))
    assert _SECRET not in rendered
    assert REDACTION in rendered


def test_secret_in_span_event_is_redacted_before_export() -> None:
    registry = _registry()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(orx.RedactingSpanExporter(exporter, registry)))
    tracer = provider.get_tracer("kdive.test.redact.span.event")

    with tracer.start_as_current_span("op") as span:
        try:
            raise ValueError(f"failed with token={_SECRET}")
        except ValueError as exc:
            span.record_exception(exc)

    spans = exporter.get_finished_spans()
    assert spans, "expected an exported span"
    rendered = "".join(str(dict(event.attributes or {})) for event in spans[0].events)
    assert _SECRET not in rendered, "exception-event message/stacktrace must be redacted"
    assert REDACTION in rendered


class _CapturingMetricExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.captured: list[MetricsData] = []

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10000, **kwargs
    ) -> MetricExportResult:
        self.captured.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30000, **kwargs) -> None:
        return None


def test_secret_in_metric_label_is_redacted_before_export() -> None:
    registry = _registry()
    capture = _CapturingMetricExporter()
    reader = PeriodicExportingMetricReader(orx.RedactingMetricExporter(capture, registry))
    provider = MeterProvider(metric_readers=[reader])
    counter = provider.get_meter("kdive.test.redact.metric").create_counter("c")

    counter.add(1, {"detail": f"token={_SECRET}"})
    provider.force_flush()

    rendered = "".join(str(d.to_json()) for d in capture.captured)
    assert capture.captured, "expected an exported metric batch"
    assert _SECRET not in rendered
    assert REDACTION in rendered


def test_registry_redactor_rebuilds_when_registry_version_changes() -> None:
    registry = SecretRegistry()
    cached = orx._RegistryRedactor(registry)

    later_secret = "sk-added-after-build"  # pragma: allowlist secret - test fixture
    registry.register(later_secret, scope=None)

    redacted = cached.current().redact_value(f"value {later_secret}")
    assert later_secret not in redacted
    assert REDACTION in redacted


def test_registry_redactor_picks_up_each_new_secret() -> None:
    registry = SecretRegistry()
    cached = orx._RegistryRedactor(registry)

    first = "sk-first-secret"  # pragma: allowlist secret - test fixture
    registry.register(first, scope=None)
    assert REDACTION in cached.current().redact_value(first)

    second = "sk-second-secret"  # pragma: allowlist secret - test fixture
    registry.register(second, scope=None)
    second_redacted = cached.current().redact_value(second)
    assert second not in second_redacted
    assert REDACTION in second_redacted


def test_registry_redactor_does_not_rebuild_when_version_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    cached = orx._RegistryRedactor(registry)
    warmed = cached.current()  # one rebuild to sync cached_version with the registry

    builds = 0
    real_redactor = orx.Redactor

    def _counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal builds
        builds += 1
        return real_redactor(*args, **kwargs)

    monkeypatch.setattr(orx, "Redactor", _counting)

    first = cached.current()
    second = cached.current()
    assert first is second is warmed
    assert builds == 0  # version did not change, so no rebuild occurred


class _RecordingMetricExporter(MetricExporter):
    def __init__(self, *, preferred_temporality: Any = None) -> None:
        super().__init__(preferred_temporality=preferred_temporality or {})
        self.export_calls: list[tuple[float, dict[str, Any]]] = []
        self.force_flush_calls: list[float] = []
        self.shutdown_calls: list[float] = []

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10000, **kwargs: Any
    ) -> MetricExportResult:
        self.export_calls.append((timeout_millis, dict(kwargs)))
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10000) -> bool:
        self.force_flush_calls.append(timeout_millis)
        return True

    def shutdown(self, timeout_millis: float = 30000, **kwargs: Any) -> None:
        self.shutdown_calls.append(timeout_millis)


def _empty_metrics_data() -> MetricsData:
    return MetricsData(resource_metrics=[])


def test_metric_exporter_delegates_export_arguments() -> None:
    inner = _RecordingMetricExporter()
    exporter = orx.RedactingMetricExporter(inner, _registry())

    result = exporter.export(_empty_metrics_data(), 1234, extra="kw")

    assert result is MetricExportResult.SUCCESS
    assert inner.export_calls == [(1234, {"extra": "kw"})]


def test_metric_exporter_force_flush_delegates_with_default() -> None:
    inner = _RecordingMetricExporter()
    exporter = orx.RedactingMetricExporter(inner, _registry())

    assert exporter.force_flush() is True
    assert inner.force_flush_calls == [10000]


def test_metric_exporter_shutdown_delegates_with_default() -> None:
    inner = _RecordingMetricExporter()
    exporter = orx.RedactingMetricExporter(inner, _registry())

    exporter.shutdown()
    assert inner.shutdown_calls == [30000]


def test_metric_exporter_copies_inner_preferred_temporality() -> None:
    from opentelemetry.sdk.metrics import Counter
    from opentelemetry.sdk.metrics.export import AggregationTemporality

    preferred = {Counter: AggregationTemporality.DELTA}
    inner = _RecordingMetricExporter(preferred_temporality=preferred)
    exporter = orx.RedactingMetricExporter(inner, _registry())

    assert exporter._preferred_temporality[Counter] is AggregationTemporality.DELTA


def test_metric_exporter_copies_inner_preferred_aggregation() -> None:
    from opentelemetry.sdk.metrics import Counter
    from opentelemetry.sdk.metrics.export import MetricExportResult
    from opentelemetry.sdk.metrics.view import LastValueAggregation

    aggregation = LastValueAggregation()

    class _AggExporter(MetricExporter):
        def __init__(self) -> None:
            super().__init__(preferred_aggregation={Counter: aggregation})

        def export(
            self, metrics_data: MetricsData, timeout_millis: float = 10000, **kwargs: Any
        ) -> MetricExportResult:
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis: float = 10000) -> bool:
            return True

        def shutdown(self, timeout_millis: float = 30000, **kwargs: Any) -> None:
            return None

    exporter = orx.RedactingMetricExporter(_AggExporter(), _registry())
    assert exporter._preferred_aggregation[Counter] is aggregation


def test_metric_exporter_skips_metric_without_data_points() -> None:
    from kdive.security.secrets.redaction import Redactor

    class _NoPoints:
        pass

    class _Metric:
        data = _NoPoints()

    class _Scope:
        metrics = [_Metric()]

    class _Resource:
        scope_metrics = [_Scope()]

    class _Data:
        resource_metrics = [_Resource()]

    orx._redact_metrics_data(Redactor(registry=_registry()), _Data())  # must not raise


def test_log_processor_force_flush_returns_true() -> None:
    processor = orx.RedactingLogProcessor(_registry())
    assert processor.force_flush() is True
    assert processor.force_flush(5000) is True


def test_span_exporter_force_flush_delegates() -> None:
    calls: list[int] = []

    class _Inner:
        def export(self, spans: Any) -> Any:
            return None

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            calls.append(timeout_millis)
            return True

    exporter = orx.RedactingSpanExporter(_Inner(), _registry())
    assert exporter.force_flush() is True
    assert calls == [30000]
