"""Cover the telemetry middleware: exception categorization + RED metric emission.

The fakes capture every metric name, label, span attribute, status, and the recorded
duration so a mutated metric name, label key, span kind, comparison, or arithmetic fails.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any

from opentelemetry.trace import SpanKind, StatusCode

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.middleware.shared import ToolOutcome
from kdive.mcp.middleware.telemetry import (
    _DURATION_BUCKETS,
    TelemetryMiddleware,
    _exception_category,
)
from kdive.mcp.responses import ToolResponse


def test_exception_category_uses_typed_category() -> None:
    exc = CategorizedError("boom", category=ErrorCategory.CONFIGURATION_ERROR)
    assert _exception_category(exc) == ErrorCategory.CONFIGURATION_ERROR.value


def test_exception_category_falls_back_to_infrastructure() -> None:
    assert _exception_category(ValueError("x")) == ErrorCategory.INFRASTRUCTURE_FAILURE.value


class _Counter:
    def __init__(self, name: str, kwargs: dict[str, Any]) -> None:
        self.name = name
        self.kwargs = kwargs
        self.calls: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, labels: dict[str, str]) -> None:
        self.calls.append((amount, dict(labels)))


class _Histogram:
    def __init__(self, name: str, kwargs: dict[str, Any]) -> None:
        self.name = name
        self.kwargs = kwargs
        self.records: list[tuple[float, dict[str, str]]] = []

    def record(self, value: float, labels: dict[str, str]) -> None:
        self.records.append((value, dict(labels)))


class _Meter:
    def __init__(self) -> None:
        self.counters: dict[str, _Counter] = {}
        self.histograms: dict[str, _Histogram] = {}

    def create_counter(self, name: str, **kwargs: Any) -> _Counter:
        counter = _Counter(name, kwargs)
        self.counters[name] = counter
        return counter

    def create_histogram(self, name: str, **kwargs: Any) -> _Histogram:
        histogram = _Histogram(name, kwargs)
        self.histograms[name] = histogram
        return histogram


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.statuses: list[Any] = []
        self.exceptions: list[BaseException] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: Any) -> None:
        self.statuses.append(status)

    def record_exception(self, exc: BaseException) -> None:
        self.exceptions.append(exc)


class _Tracer:
    def __init__(self) -> None:
        self.span = _Span()
        self.span_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @contextmanager
    def start_as_current_span(self, *args: Any, **kwargs: Any):
        self.span_calls.append((args, kwargs))
        yield self.span


class _Message:
    def __init__(self, name: str) -> None:
        self.name = name


class _Context:
    def __init__(self, name: str) -> None:
        self.message = _Message(name)


def _middleware() -> tuple[TelemetryMiddleware, _Meter, _Tracer]:
    meter = _Meter()
    tracer = _Tracer()
    return TelemetryMiddleware(tracer=tracer, meter=meter), meter, tracer


def test_init_registers_named_instruments() -> None:
    _mw, meter, _ = _middleware()
    requests = meter.counters["kdive.mcp.requests"]
    assert requests.kwargs["unit"] == "1"
    assert requests.kwargs["description"] == "MCP tool calls dispatched."
    errors = meter.counters["kdive.mcp.request.errors"]
    assert errors.kwargs["unit"] == "1"
    assert errors.kwargs["description"] == "MCP tool calls that failed."
    hist = meter.histograms["kdive.mcp.request.duration"]
    assert hist.kwargs["unit"] == "s"
    assert hist.kwargs["description"] == "MCP tool-call wall-clock duration."
    assert hist.kwargs["explicit_bucket_boundaries_advisory"] == list(_DURATION_BUCKETS)


def test_success_records_ok_outcome_and_no_error() -> None:
    mw, meter, tracer = _middleware()
    seen: list[Any] = []

    async def call_next(ctx: Any) -> ToolResponse:
        seen.append(ctx)
        return ToolResponse.success("runs.create", "created")

    ctx = _Context("runs.create")
    asyncio.run(mw.on_call_tool(ctx, call_next))

    assert seen == [ctx]  # call_next received the real context, not None
    assert meter.counters["kdive.mcp.requests"].calls == [
        (1, {"tool": "runs.create", "outcome": "ok"})
    ]
    assert meter.counters["kdive.mcp.request.errors"].calls == []
    (value, labels) = meter.histograms["kdive.mcp.request.duration"].records[0]
    assert labels == {"tool": "runs.create", "outcome": "ok"}
    assert value >= 0.0  # a flipped perf_counter sign would make this large/negative
    assert value < 60.0
    assert tracer.span.attributes["outcome"] == "ok"
    # span opened with the tool-scoped name, SERVER kind, and the tool attribute
    (span_args, span_kwargs) = tracer.span_calls[0]
    assert span_args == ("mcp.tool/runs.create",)
    assert span_kwargs["kind"] is SpanKind.SERVER
    assert span_kwargs["attributes"] == {"tool": "runs.create"}
    assert tracer.span.statuses == []  # success sets no error status


def test_failure_envelope_records_error_with_category_and_status() -> None:
    mw, meter, tracer = _middleware()

    async def call_next(_ctx: Any) -> ToolResponse:
        return ToolResponse.failure("runs.create", ErrorCategory.CONFIGURATION_ERROR)

    asyncio.run(mw.on_call_tool(_Context("runs.create"), call_next))

    assert meter.counters["kdive.mcp.requests"].calls == [
        (1, {"tool": "runs.create", "outcome": "error"})
    ]
    assert meter.counters["kdive.mcp.request.errors"].calls == [
        (1, {"tool": "runs.create", "outcome": "error", "error_category": "configuration_error"})
    ]
    assert [s.status_code for s in tracer.span.statuses] == [StatusCode.ERROR]


def test_raised_exception_records_error_status_exc_and_reraises() -> None:
    mw, meter, tracer = _middleware()
    boom = CategorizedError("boom", category=ErrorCategory.BUILD_FAILURE)

    async def call_next(_ctx: Any) -> ToolResponse:
        raise boom

    try:
        asyncio.run(mw.on_call_tool(_Context("runs.create"), call_next))
    except CategorizedError as exc:
        caught = exc
    else:
        caught = None

    assert caught is boom  # the original exception propagates unchanged
    assert meter.counters["kdive.mcp.requests"].calls == [
        (1, {"tool": "runs.create", "outcome": "error"})
    ]
    assert meter.counters["kdive.mcp.request.errors"].calls == [
        (1, {"tool": "runs.create", "outcome": "error", "error_category": "build_failure"})
    ]
    assert tracer.span.exceptions == [boom]  # the actual exc, not None
    assert [s.status_code for s in tracer.span.statuses] == [StatusCode.ERROR]


def test_record_error_without_category_omits_label() -> None:
    mw, meter, _ = _middleware()
    mw._record_error("runs.create", None)
    assert meter.counters["kdive.mcp.request.errors"].calls == [
        (1, {"tool": "runs.create", "outcome": ToolOutcome.ERROR.value})
    ]


def test_record_error_with_category_includes_label() -> None:
    mw, meter, _ = _middleware()
    mw._record_error("runs.create", "build_failure")
    assert meter.counters["kdive.mcp.request.errors"].calls == [
        (1, {"tool": "runs.create", "outcome": "error", "error_category": "build_failure"})
    ]
