"""Report a server fault in a tool body as one, not as an argument error (ADR-0709).

An invalid ``ToolResponse`` and a failed rebuild of stored data raise
:class:`~kdive.mcp.responses.ServerFaultError` rather than a pydantic ``ValidationError``, so
FastMCP logs it at ERROR with its traceback — the server-side record — and re-raises it as a
``ToolError`` chained from it. This middleware turns that ``ToolError`` into an
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
from kdive.mcp.responses import ServerFaultError, ToolResponse

SERVER_FAULT_DETAIL = "the server could not build this tool's response"


class ServerFaultMiddleware(Middleware):
    """Convert a tool's server fault into an ``infrastructure_failure`` envelope."""

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one call; envelope a ``ToolError`` caused by a server fault."""
        try:
            return await call_next(context)
        except ToolError as exc:
            if not isinstance(exc.__cause__, ServerFaultError):
                raise
        envelope = ToolResponse.failure(
            str(context.message.name),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            detail=SERVER_FAULT_DETAIL,
        )
        return ToolResult(structured_content=envelope.model_dump(mode="json"))
