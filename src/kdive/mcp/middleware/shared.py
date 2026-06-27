"""Shared helpers for MCP middleware modules."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse


class ToolOutcome(StrEnum):
    """Normalized outcome labels used by middleware metrics and usage rows."""

    OK = "ok"
    ERROR = "error"
    DENIED = "denied"


def request_context() -> Any:
    """Return the current request context through the middleware-local patch point."""
    return current_context()


def result_error_category(result: Any) -> str | None:
    """Return the envelope ``error_category`` from a ToolResult or ToolResponse."""
    if isinstance(result, ToolResponse):
        return result.error_category
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        value = structured.get("error_category")
        return value if isinstance(value, str) else None
    return None
