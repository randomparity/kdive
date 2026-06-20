"""Telemetry middleware for MCP tool calls."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import Middleware
from opentelemetry.trace import SpanKind, Status, StatusCode

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.middleware.shared import ToolOutcome, result_error_category

_DURATION_BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


def _exception_category(exc: BaseException) -> str:
    """The error_category for a raised exception: the typed category, else infrastructure."""
    if isinstance(exc, CategorizedError):
        return exc.category.value
    return ErrorCategory.INFRASTRUCTURE_FAILURE.value


class TelemetryMiddleware(Middleware):
    """Emit a span and per-tool RED metrics for every MCP tool call."""

    def __init__(self, *, tracer: Any, meter: Any) -> None:
        self._tracer = tracer
        self._requests = meter.create_counter(
            "kdive.mcp.requests", unit="1", description="MCP tool calls dispatched."
        )
        self._errors = meter.create_counter(
            "kdive.mcp.request.errors", unit="1", description="MCP tool calls that failed."
        )
        self._duration = meter.create_histogram(
            "kdive.mcp.request.duration",
            unit="s",
            description="MCP tool-call wall-clock duration.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Time and trace one tool call; record RED metrics; re-raise on failure."""
        tool = context.message.name
        started = time.perf_counter()
        with self._tracer.start_as_current_span(
            f"mcp.tool/{tool}", kind=SpanKind.SERVER, attributes={"tool": tool}
        ) as span:
            try:
                result = await call_next(context)
            except Exception as exc:
                self._finish(span, tool, ToolOutcome.ERROR, started)
                self._record_error(tool, _exception_category(exc))
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            category = result_error_category(result)
            outcome = ToolOutcome.ERROR if category is not None else ToolOutcome.OK
            self._finish(span, tool, outcome, started)
            if outcome is ToolOutcome.ERROR:
                self._record_error(tool, category)
                span.set_status(Status(StatusCode.ERROR))
            return result

    def _record_error(self, tool: str, category: str | None) -> None:
        # ADR-0190 E: break the per-call error rate down by error_category (the request
        # surface's by-category counter; backend-origin failures live on kdive.errors).
        labels = {"tool": tool, "outcome": ToolOutcome.ERROR.value}
        if category is not None:
            labels["error_category"] = category
        self._errors.add(1, labels)

    def _finish(self, span: Any, tool: str, outcome: ToolOutcome, started: float) -> None:
        labels = {"tool": tool, "outcome": outcome.value}
        span.set_attribute("outcome", outcome.value)
        self._requests.add(1, labels)
        self._duration.record(time.perf_counter() - started, labels)
