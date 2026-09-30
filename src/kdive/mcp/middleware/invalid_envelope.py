"""Report an invalid tool-response envelope as a server fault (ADR-0709).

``ToolResponse`` raises :class:`~kdive.mcp.responses.InvalidEnvelopeError` rather than a pydantic
``ValidationError``, so FastMCP logs it at ERROR with its traceback — the server-side record — and
re-raises it as a ``ToolError`` chained from it. This middleware turns that ``ToolError`` into an
``infrastructure_failure`` envelope whose ``detail`` is a fixed constant, so nothing about the
fault reaches the caller (ADR-0123). Registered innermost, so every other middleware observes an
ordinary failure envelope.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import InvalidEnvelopeError, ToolResponse

INVALID_ENVELOPE_DETAIL = "the server could not build this tool's response"


class InvalidEnvelopeMiddleware(Middleware):
    """Convert a tool's invalid-envelope fault into an ``infrastructure_failure`` envelope."""

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one call; envelope a ``ToolError`` caused by an invalid envelope."""
        try:
            return await call_next(context)
        except ToolError as exc:
            if not isinstance(exc.__cause__, InvalidEnvelopeError):
                raise
        envelope = ToolResponse.failure(
            str(context.message.name),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            detail=INVALID_ENVELOPE_DETAIL,
        )
        return ToolResult(structured_content=envelope.model_dump(mode="json"))
