"""The stdout log exporter preserves the ADR-0014 schema + additive trace fields.

ADR-0090 §2: every ADR-0014 field keeps its name/meaning so existing consumers and
the log tests are unbroken, and `trace_id`/`span_id` are added so an operator on the
stdout path (`kubectl logs`/`journalctl`) can correlate a record to its trace.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import sys
from typing import Any

from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider, ReadableLogRecord
from opentelemetry.sdk._logs._internal import LogRecord
from opentelemetry.sdk._logs.export import LogRecordExportResult, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from kdive.observability.stdout_exporter import (
    StdoutJsonLogExporter,
    format_log_record_json,
)

_ADR0014_FIELDS = ("ts", "level", "logger", "msg")

_FIXED_TS_NS = 1_700_000_000_000_000_000
_FIXED_TS_ISO = "2023-11-14T22:13:20+00:00"
_TRACE_ID = 0x0123456789ABCDEF0123456789ABCDEF
_SPAN_ID = 0xFEDCBA9876543210


def _make_record(
    *,
    timestamp: int | None = _FIXED_TS_NS,
    observed_timestamp: int | None = None,
    severity_text: str | None = "WARNING",
    body: object | None = "hello",
    trace_id: int | None = _TRACE_ID,
    span_id: int | None = _SPAN_ID,
    attributes: dict[str, Any] | None = None,
    scope_name: str | None = "my.logger",
) -> ReadableLogRecord:
    log_record = LogRecord(
        timestamp=timestamp,
        observed_timestamp=observed_timestamp,
        severity_text=severity_text,
        body=body,
        trace_id=trace_id,
        span_id=span_id,
        attributes=attributes or {},
    )
    scope = InstrumentationScope(scope_name) if scope_name is not None else None
    return ReadableLogRecord(
        log_record=log_record,
        resource=Resource.create({}),
        instrumentation_scope=scope,
    )


def _format(**kwargs: Any) -> dict:
    return json.loads(format_log_record_json(_make_record(**kwargs)))


def _emit(logger_name: str, span: bool = False) -> dict:
    lines: list[str] = []
    log_provider = LoggerProvider()
    log_provider.add_log_record_processor(
        SimpleLogRecordProcessor(StdoutJsonLogExporter(write=lines.append))
    )
    handler = LoggingHandler(logger_provider=log_provider)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if span:
        tracer = TracerProvider().get_tracer("t")
        with tracer.start_as_current_span("s"):
            logger.info("hello in span")
    else:
        logger.info("hello plain")
    log_provider.force_flush()
    return json.loads([line for line in lines if line.strip()][-1])


def test_stdout_carries_adr0014_fields() -> None:
    record = _emit("kdive.test.stdout.fields")
    for field in _ADR0014_FIELDS:
        assert field in record, f"ADR-0014 field {field} missing"
    assert record["msg"] == "hello plain"
    assert record["level"] == "INFO"


def test_stdout_carries_trace_id_under_active_span() -> None:
    record = _emit("kdive.test.stdout.trace", span=True)
    assert "trace_id" in record
    assert "span_id" in record
    assert record["trace_id"], "trace_id should be a non-empty hex string under a span"
    assert int(record["trace_id"], 16) != 0


def test_stdout_trace_id_empty_without_span() -> None:
    record = _emit("kdive.test.stdout.notrace")
    # The field is always present (stable schema) but empty outside a span.
    assert record.get("trace_id", "") == ""
    assert record.get("span_id", "") == ""


def test_format_helper_emits_single_json_object() -> None:
    log_provider = LoggerProvider()
    captured: list[object] = []
    log_provider.add_log_record_processor(
        SimpleLogRecordProcessor(StdoutJsonLogExporter(write=lambda _: None))
    )
    handler = LoggingHandler(logger_provider=log_provider)
    logger = logging.getLogger("kdive.test.stdout.helper")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    captured.clear()
    log_provider.add_log_record_processor(
        SimpleLogRecordProcessor(StdoutJsonLogExporter(write=captured.append))
    )
    logger.info("payload")
    log_provider.force_flush()
    rendered = [c for c in captured if isinstance(c, str) and c.strip()][-1]
    parsed = json.loads(rendered)
    assert parsed["msg"] == "payload"
    assert "\n" not in rendered.strip()


def test_format_renders_timestamp_from_record_timestamp_in_utc() -> None:
    record = _format()
    assert record["ts"] == _FIXED_TS_ISO
    parsed = _dt.datetime.fromisoformat(record["ts"])
    assert parsed.utcoffset() == _dt.timedelta(0)


def test_format_falls_back_to_observed_timestamp() -> None:
    record = _format(timestamp=None, observed_timestamp=_FIXED_TS_NS)
    assert record["ts"] == _FIXED_TS_ISO


def test_format_unset_timestamps_render_epoch() -> None:
    record = _format(timestamp=None, observed_timestamp=0)
    assert record["ts"] == "1970-01-01T00:00:00+00:00"


def test_format_preserves_explicit_level() -> None:
    record = _format(severity_text="WARNING")
    assert record["level"] == "WARNING"


def test_format_missing_level_defaults_to_info() -> None:
    record = _format(severity_text=None)
    assert record["level"] == "INFO"


def test_format_logger_from_instrumentation_scope() -> None:
    record = _format(scope_name="kdive.svc.alpha")
    assert record["logger"] == "kdive.svc.alpha"


def test_format_logger_empty_without_scope() -> None:
    record = _format(scope_name=None)
    assert record["logger"] == ""


def test_format_msg_from_body() -> None:
    record = _format(body="provisioned system-7")
    assert record["msg"] == "provisioned system-7"


def test_format_msg_empty_when_body_none() -> None:
    record = _format(body=None)
    assert record["msg"] == ""


def test_format_includes_stacktrace_as_exc() -> None:
    record = _format(attributes={"exception.stacktrace": "Traceback (most recent call last): boom"})
    assert record["exc"] == "Traceback (most recent call last): boom"


def test_format_omits_exc_without_stacktrace() -> None:
    record = _format(attributes={})
    assert "exc" not in record


def test_format_ignores_non_string_stacktrace() -> None:
    record = _format(attributes={"exception.stacktrace": 12345})
    assert "exc" not in record


def test_format_trace_id_is_32_hex_chars() -> None:
    record = _format(trace_id=_TRACE_ID)
    assert record["trace_id"] == f"{_TRACE_ID:032x}"
    assert len(record["trace_id"]) == 32


def test_format_span_id_is_16_hex_chars() -> None:
    record = _format(span_id=_SPAN_ID)
    assert record["span_id"] == f"{_SPAN_ID:016x}"
    assert len(record["span_id"]) == 16


def test_format_trace_and_span_empty_when_unset() -> None:
    record = _format(trace_id=0, span_id=0)
    assert record["trace_id"] == ""
    assert record["span_id"] == ""


def test_format_flattens_bound_context_fields() -> None:
    record = _format(attributes={"_kdive_ctx": {"request_id": "req-9", "principal": "alice"}})
    assert record["request_id"] == "req-9"
    assert record["principal"] == "alice"


def test_format_lifts_top_level_context_attributes() -> None:
    record = _format(attributes={"job_id": "job-42"})
    assert record["job_id"] == "job-42"


def test_format_coerces_non_serializable_values() -> None:
    object_id = _dt.datetime(2023, 1, 1, tzinfo=_dt.UTC)
    record = _format(attributes={"_kdive_ctx": {"object_id": object_id}})
    assert record["object_id"] == str(object_id)


def test_exporter_writes_each_line_to_out_stream() -> None:
    stream = io.StringIO()
    exporter = StdoutJsonLogExporter(out=stream)
    result = exporter.export([_make_record(body="line-one")])
    assert result is LogRecordExportResult.SUCCESS
    written = stream.getvalue()
    assert written.endswith("\n")
    parsed = json.loads(written.strip())
    assert parsed["msg"] == "line-one"


def test_exporter_flushes_out_stream_when_no_write_sink() -> None:
    flushed: list[bool] = []

    class _RecordingStream(io.StringIO):
        def flush(self) -> None:
            flushed.append(True)
            super().flush()

    exporter = StdoutJsonLogExporter(out=_RecordingStream())
    exporter.export([_make_record()])
    assert flushed == [True]


def test_exporter_write_sink_bypasses_stream_and_flush() -> None:
    lines: list[str] = []
    flushed: list[bool] = []

    class _RecordingStream(io.StringIO):
        def flush(self) -> None:
            flushed.append(True)
            super().flush()

    exporter = StdoutJsonLogExporter(out=_RecordingStream(), write=lines.append)
    exporter.export([_make_record(body="sink")])
    assert flushed == []
    assert len(lines) == 1
    assert json.loads(lines[0])["msg"] == "sink"


def test_exporter_defaults_out_to_stderr() -> None:
    exporter = StdoutJsonLogExporter()
    assert exporter._out is sys.stderr


def test_force_flush_returns_true() -> None:
    assert StdoutJsonLogExporter(write=lambda _: None).force_flush() is True
