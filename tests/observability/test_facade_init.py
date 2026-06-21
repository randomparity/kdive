"""End-to-end facade wiring (ADR-0090 §1, §2): bootstrap ordering + stdout floor.

Asserts the acceptance criteria that live at the facade boundary: a registered secret
is redacted in the stdout output the running process actually emits, the stdlib floor
is installed first and then handed over to the OTel bridge (no doubling), and OTLP
stays off by default.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogExportResult,
    LogRecordExporter,
)
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

import kdive.config as config
from kdive.config.core_settings import OTEL_EXPORTER_OTLP_ENDPOINT
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import _KdiveHandler
from kdive.observability import facade
from kdive.observability.facade import (
    LoggingHandler,
    Telemetry,
    _sampler,
    bootstrap_stdout_floor,
    init_telemetry,
    otlp_enabled,
)
from kdive.observability.redaction import RedactingMetricExporter, RedactingSpanExporter
from kdive.security.secrets.secret_registry import SecretRegistry

_SERVICE_NAME_KEY = "service.name"
_SERVICE_NAMESPACE_KEY = "service.namespace"


def _assert_ratio_sampler(sampler: object, expected_ratio: float) -> None:
    """Assert a ParentBased(root=TraceIdRatioBased(ratio)) sampler at the given ratio.

    Checks the load-bearing behavior — the ParentBased wrapping and the root ratio —
    via the SDK's public sampler types rather than its formatted description string, so
    an OTel description-format change does not produce a false failure.
    """
    assert isinstance(sampler, ParentBased)
    root = sampler._root  # noqa: SLF001 - OTel exposes the root only privately
    assert isinstance(root, TraceIdRatioBased)
    assert root.rate == expected_ratio


@pytest.fixture(autouse=True)
def _restore_root() -> Iterator[None]:
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    yield
    root.handlers = before
    root.setLevel(before_level)


def test_bootstrap_floor_installs_stdlib_handler_first() -> None:
    config.load({})
    bootstrap_stdout_floor("INFO", secret_registry=SecretRegistry())
    root = logging.getLogger()
    assert any(isinstance(h, _KdiveHandler) for h in root.handlers)


def test_init_telemetry_replaces_floor_with_bridge() -> None:
    config.load({})
    registry = SecretRegistry()
    bootstrap_stdout_floor("INFO", secret_registry=registry)
    init_telemetry("server", secret_registry=registry, level="INFO")
    root = logging.getLogger()
    assert not any(isinstance(h, _KdiveHandler) for h in root.handlers), "floor must be removed"
    assert any(isinstance(h, LoggingHandler) for h in root.handlers), "bridge must be installed"


def _meter_resource_attrs(telemetry: Telemetry) -> dict[str, object]:
    # OTel-internal coupling: the MeterProvider exposes neither its resource nor its
    # readers publicly, so both accessors reach through the private _sdk_config. Isolated
    # here so a future public accessor (or SDK rename) is a one-line change.
    return dict(telemetry.meter_provider._sdk_config.resource.attributes)


def _metric_readers(telemetry: Telemetry) -> list[object]:
    # OTel-internal coupling (see _meter_resource_attrs).
    return list(telemetry.meter_provider._sdk_config.metric_readers)


def test_init_telemetry_threads_service_name_into_every_provider_resource() -> None:
    config.load({})
    telemetry = init_telemetry("server", secret_registry=SecretRegistry(), level="INFO")

    for attrs in (
        dict(telemetry.tracer_provider.resource.attributes),
        dict(telemetry.logger_provider.resource.attributes),
        _meter_resource_attrs(telemetry),
    ):
        assert attrs[_SERVICE_NAME_KEY] == "server"
        assert attrs[_SERVICE_NAMESPACE_KEY] == "kdive"


def test_init_telemetry_returns_distinct_live_providers() -> None:
    config.load({})
    telemetry = init_telemetry("worker", secret_registry=SecretRegistry(), level="INFO")

    assert telemetry.meter_provider is not None
    assert telemetry.tracer_provider is not None
    assert telemetry.logger_provider is not None
    # The tracer provider carries the configured ratio sampler, not the SDK default (which a
    # dropped/None sampler argument would silently substitute).
    _assert_ratio_sampler(telemetry.tracer_provider.sampler, 0.1)


def test_init_telemetry_attaches_scrape_reader_to_meter_provider() -> None:
    # The aux /metrics endpoint renders the in-memory scrape reader; it must be wired onto the
    # meter provider's readers or a Prometheus pull would see nothing.
    config.load({})
    telemetry = init_telemetry("worker", secret_registry=SecretRegistry(), level="INFO")

    assert telemetry.scrape_reader in _metric_readers(telemetry)


def test_init_telemetry_registers_returned_providers_globally(monkeypatch) -> None:
    # init must publish the providers it built (not None) as the process-global providers so
    # library instrumentation resolves the same pipeline.
    config.load({})
    meter_set: list[object] = []
    tracer_set: list[object] = []
    logger_set: list[object] = []
    monkeypatch.setattr(facade.metrics, "set_meter_provider", meter_set.append)
    monkeypatch.setattr(facade.trace, "set_tracer_provider", tracer_set.append)
    monkeypatch.setattr(facade, "set_logger_provider", logger_set.append)

    telemetry = init_telemetry("worker", secret_registry=SecretRegistry(), level="INFO")

    assert meter_set == [telemetry.meter_provider]
    assert tracer_set == [telemetry.tracer_provider]
    assert logger_set == [telemetry.logger_provider]


def test_sampler_uses_configured_ratio() -> None:
    config.load({"KDIVE_OTEL_TRACES_SAMPLER_RATIO": "0.5"})
    _assert_ratio_sampler(_sampler(), 0.5)


def test_sampler_defaults_to_one_tenth_when_unset() -> None:
    config.load({})
    _assert_ratio_sampler(_sampler(), 0.1)


def test_sampler_falls_back_to_one_tenth_when_config_returns_none(monkeypatch) -> None:
    # Defensive fallback: if config ever yields no ratio, the sampler must still use the
    # documented 0.1 default rather than passing None into TraceIdRatioBased.
    config.load({})
    monkeypatch.setattr(facade.config, "get", lambda _setting: None)
    _assert_ratio_sampler(_sampler(), 0.1)


def test_resource_falls_back_to_kdive_namespace_when_config_returns_none(monkeypatch) -> None:
    # Defensive fallback: a missing namespace must resolve to "kdive", not an empty/None
    # service.namespace that would leave exported telemetry unscoped.
    config.load({})
    monkeypatch.setattr(facade.config, "get", lambda _setting: None)
    resource = facade._resource("server")
    assert dict(resource.attributes)[_SERVICE_NAMESPACE_KEY] == "kdive"


def test_otlp_enabled_true_for_truthy_value() -> None:
    config.load({"KDIVE_OTEL_ENABLED": "true"})
    assert otlp_enabled() is True


def test_otlp_enabled_false_for_non_truthy_value() -> None:
    # A set-but-unrecognized value must read as off, not on (the membership check, not its
    # negation, decides).
    config.load({"KDIVE_OTEL_ENABLED": "garbage"})
    assert otlp_enabled() is False


def test_otlp_enabled_false_when_unset() -> None:
    config.load({})
    assert otlp_enabled() is False


def test_resource_uses_configured_namespace() -> None:
    config.load({"KDIVE_OTEL_SERVICE_NAMESPACE": "acme"})
    telemetry = init_telemetry("server", secret_registry=SecretRegistry(), level="INFO")

    assert dict(telemetry.tracer_provider.resource.attributes)[_SERVICE_NAMESPACE_KEY] == "acme"


def test_bootstrap_floor_applies_requested_level() -> None:
    config.load({})
    bootstrap_stdout_floor("DEBUG", secret_registry=SecretRegistry())

    assert logging.getLogger().level == logging.DEBUG


def test_bridge_root_logger_sets_configured_level() -> None:
    config.load({})
    bootstrap_stdout_floor("INFO", secret_registry=SecretRegistry())
    init_telemetry("server", secret_registry=SecretRegistry(), level="WARNING")

    assert logging.getLogger().level == logging.WARNING


def test_bridge_root_logger_falls_back_to_info_for_unknown_level() -> None:
    # An unrecognized level name must fall back to INFO, not to a None level that breaks the
    # root logger's effective-level resolution.
    config.load({})
    bootstrap_stdout_floor("INFO", secret_registry=SecretRegistry())
    init_telemetry("server", secret_registry=SecretRegistry(), level="NOPE")

    assert logging.getLogger().level == logging.INFO


def test_registered_secret_is_redacted_in_stdout(monkeypatch) -> None:
    config.load({})
    registry = SecretRegistry()
    registry.register("hunter2-prod-token", scope=None)
    stream = io.StringIO()
    # The stdout exporter binds sys.stderr at construction, so patch before init.
    monkeypatch.setattr("sys.stderr", stream)
    telemetry = init_telemetry("worker", secret_registry=registry, level="INFO")
    logging.getLogger("kdive.test.facade").info("using hunter2-prod-token to connect")
    telemetry.logger_provider.force_flush()
    output = stream.getvalue()
    assert output, "expected the stdout exporter to emit a line"
    record = json.loads(output.splitlines()[-1])
    assert "hunter2-prod-token" not in output
    assert "[REDACTED]" in record["msg"]
    assert record["logger"] == "kdive.test.facade"


_OTLP_ENDPOINT = "collector.example:4317"
_OTLP_ON_CONFIG = {
    "KDIVE_OTEL_ENABLED": "1",
    "KDIVE_OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{_OTLP_ENDPOINT}",
    # Force every span through the exporter so the redaction assertion is deterministic;
    # the production default ratio would drop most root spans before export.
    "KDIVE_OTEL_TRACES_SAMPLER_RATIO": "1",
}
_SECRET = "hunter2-prod-token"
_SECRET_BEARING_VALUE = f"postgres://user:{_SECRET}@db.internal/app"


class _FakeOtlpSpanExporter(SpanExporter):
    """Stand-in for OTLPSpanExporter that records the spans handed to it for export."""

    def __init__(self, *, endpoint: str | None = None, **_kwargs: object) -> None:
        self.endpoint = endpoint
        self.exported: list[object] = []

    def export(self, spans):  # type: ignore[no-untyped-def]
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class _FakeOtlpMetricExporter(MetricExporter):
    """Stand-in for OTLPMetricExporter that records the metrics data handed to it."""

    def __init__(self, *, endpoint: str | None = None, **_kwargs: object) -> None:
        super().__init__()
        self.endpoint = endpoint
        self.exported: list[object] = []

    def export(self, metrics_data, timeout_millis: float = 10000, **_kwargs: object):  # type: ignore[no-untyped-def]
        self.exported.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30000, **_kwargs: object) -> None:
        return None


class _FakeOtlpLogExporter(LogRecordExporter):
    """Stand-in for OTLPLogExporter that records its endpoint for assertion."""

    def __init__(self, *, endpoint: str | None = None, **_kwargs: object) -> None:
        self.endpoint = endpoint
        self.exported: list[object] = []

    def export(self, batch):  # type: ignore[no-untyped-def]
        self.exported.extend(batch)
        return LogExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@pytest.fixture
def _otlp_seam(monkeypatch) -> dict[str, object]:
    """OTLP enabled, global registration stubbed, OTLP exporters replaced with fakes.

    Returns the last-built fake span/metric exporters so a test can drive a span/metric
    through the real provider pipeline and assert what reached the export boundary. The
    set_*_provider calls are stubbed so the OTLP-on run never clobbers the process-global
    pipeline for the rest of the suite.
    """
    config.load(_OTLP_ON_CONFIG)
    monkeypatch.setattr(facade.metrics, "set_meter_provider", lambda _provider: None)
    monkeypatch.setattr(facade.trace, "set_tracer_provider", lambda _provider: None)
    monkeypatch.setattr(facade, "set_logger_provider", lambda _provider: None)

    built: dict[str, object] = {}

    def make_span(*, endpoint=None, **_kwargs):
        built["span"] = _FakeOtlpSpanExporter(endpoint=endpoint)
        return built["span"]

    def make_metric(*, endpoint=None, **_kwargs):
        built["metric"] = _FakeOtlpMetricExporter(endpoint=endpoint)
        return built["metric"]

    def make_log(*, endpoint=None, **_kwargs):
        built["log"] = _FakeOtlpLogExporter(endpoint=endpoint)
        return built["log"]

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter", make_span
    )
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter", make_metric
    )
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc._log_exporter.OTLPLogExporter", make_log
    )
    return built


def _span_processors(telemetry: Telemetry) -> list[object]:
    return list(telemetry.tracer_provider._active_span_processor._span_processors)


def _log_processors(telemetry: Telemetry) -> list[object]:
    return list(telemetry.logger_provider._multi_log_record_processor._log_record_processors)


def test_otlp_on_wraps_and_targets_span_exporter(_otlp_seam: dict[str, object]) -> None:
    # The OTLP span push must leave through a RedactingSpanExporter wrapping the OTLP
    # exporter, and the exporter must be aimed at the configured endpoint (a dropped wrap
    # or a wrong/None endpoint is a real leak or misroute, not an equivalent mutant).
    registry = SecretRegistry()
    telemetry = init_telemetry("server", secret_registry=registry, level="INFO")

    batch = [p for p in _span_processors(telemetry) if isinstance(p, BatchSpanProcessor)]
    assert len(batch) == 1
    exporter = batch[0].span_exporter
    assert isinstance(exporter, RedactingSpanExporter)
    inner = _otlp_seam["span"]
    assert exporter._inner is inner  # noqa: SLF001
    assert isinstance(inner, _FakeOtlpSpanExporter)
    assert inner.endpoint == f"http://{_OTLP_ENDPOINT}"


def test_otlp_on_redacts_secret_from_exported_span(_otlp_seam: dict[str, object]) -> None:
    # End-to-end: a registered secret in a span attribute must be scrubbed before the span
    # reaches the OTLP exporter. Kills a None secret_registry threaded into the redactor.
    registry = SecretRegistry()
    registry.register(_SECRET, scope=None)
    telemetry = init_telemetry("server", secret_registry=registry, level="INFO")

    # Guard before driving a span: a dropped wrap (None span exporter) would make the
    # downstream flush hang on a broken batch worker; fail fast on the wiring instead.
    batch = [p for p in _span_processors(telemetry) if isinstance(p, BatchSpanProcessor)]
    assert len(batch) == 1
    assert isinstance(batch[0].span_exporter, RedactingSpanExporter)

    span = telemetry.tracer_provider.get_tracer("kdive.test").start_span("op")
    span.set_attribute("db.connection_string", _SECRET_BEARING_VALUE)
    span.end()
    assert telemetry.tracer_provider.force_flush(5000)

    inner = _otlp_seam["span"]
    assert isinstance(inner, _FakeOtlpSpanExporter)
    assert len(inner.exported) == 1
    exported_value = dict(inner.exported[0].attributes)["db.connection_string"]
    assert _SECRET not in exported_value
    assert "[REDACTED]" in exported_value


def test_otlp_on_wraps_and_targets_metric_exporter(_otlp_seam: dict[str, object]) -> None:
    # Same boundary for the metric push: the periodic reader must export through a
    # RedactingMetricExporter wrapping the OTLP metric exporter at the configured endpoint.
    registry = SecretRegistry()
    telemetry = init_telemetry("server", secret_registry=registry, level="INFO")

    readers = [
        r for r in _metric_readers(telemetry) if isinstance(r, PeriodicExportingMetricReader)
    ]
    assert len(readers) == 1
    exporter = readers[0]._exporter  # noqa: SLF001
    assert isinstance(exporter, RedactingMetricExporter)
    inner = _otlp_seam["metric"]
    assert exporter._inner is inner  # noqa: SLF001
    assert isinstance(inner, _FakeOtlpMetricExporter)
    assert inner.endpoint == f"http://{_OTLP_ENDPOINT}"


def test_otlp_on_redacts_secret_from_exported_metric(_otlp_seam: dict[str, object]) -> None:
    # End-to-end: a registered secret in a metric label must be scrubbed before the metric
    # data reaches the OTLP exporter. Kills a None secret_registry threaded into the redactor.
    registry = SecretRegistry()
    registry.register(_SECRET, scope=None)
    telemetry = init_telemetry("server", secret_registry=registry, level="INFO")

    counter = telemetry.meter_provider.get_meter("kdive.test").create_counter("requests")
    counter.add(1, {"db.connection_string": _SECRET_BEARING_VALUE})
    assert telemetry.meter_provider.force_flush(5000)

    inner = _otlp_seam["metric"]
    assert isinstance(inner, _FakeOtlpMetricExporter)
    assert inner.exported
    metrics_data = inner.exported[-1]
    point = metrics_data.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points[0]
    exported_value = dict(point.attributes)["db.connection_string"]
    assert _SECRET not in exported_value
    assert "[REDACTED]" in exported_value


def test_otlp_on_adds_batch_log_processor_targeting_endpoint(
    _otlp_seam: dict[str, object],
) -> None:
    # The OTLP log push adds a BatchLogRecordProcessor (alongside the always-on stdout
    # exporter) whose exporter is the OTLP log exporter aimed at the configured endpoint;
    # a None exporter or None endpoint would silently drop or misroute the OTLP log push.
    telemetry = init_telemetry("server", secret_registry=SecretRegistry(), level="INFO")
    batch = [p for p in _log_processors(telemetry) if isinstance(p, BatchLogRecordProcessor)]
    assert len(batch) == 1
    exporter = batch[0]._batch_processor._exporter  # noqa: SLF001
    assert exporter is _otlp_seam["log"]
    assert isinstance(exporter, _FakeOtlpLogExporter)
    assert exporter.endpoint == f"http://{_OTLP_ENDPOINT}"


def test_otlp_on_missing_endpoint_fails_fast(monkeypatch) -> None:
    # Enabling OTLP without an endpoint must fail loudly during init, not silently build a
    # provider with a misconfigured exporter. The raised error must be a configuration error
    # whose actionable details name the offending variable and a suggested value, so an
    # operator can fix it without reading the source.
    config.load({"KDIVE_OTEL_ENABLED": "1"})
    monkeypatch.setattr(facade.metrics, "set_meter_provider", lambda _provider: None)
    with pytest.raises(CategorizedError) as caught:
        init_telemetry("server", secret_registry=SecretRegistry(), level="INFO")

    error = caught.value
    assert str(error) == "KDIVE_OTEL_ENABLED is set but KDIVE_OTEL_EXPORTER_OTLP_ENDPOINT is not"
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details["variable"] == OTEL_EXPORTER_OTLP_ENDPOINT.name
    assert error.details["suggest"] == OTEL_EXPORTER_OTLP_ENDPOINT.suggest
