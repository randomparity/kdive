"""Shared helpers for MCP middleware modules."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse

# Two skip sets, because de-duplication and volume control are different reasons and one
# set carrying both stated an invariant that was false of half its membership (ADR-0485,
# #1654; amends ADR-0268 §6, #866). A tool joins the set whose reason it satisfies.

# De-duplication: each name re-enters the middleware chain via
# app.call_tool(run_middleware=True), so the inner chain is the authoritative record — including
# for an unknown inner name, since FastMCP runs the chain before resolution and resolves inside
# call_next. Without the skip the outer chain double-counts every usage/telemetry row. The
# denial plane is the exception and skips for a different reason (see denial_audit.py): its
# inner instance returns an enveloped denial instead of re-raising, so there is no second row
# to remove there.
REENTRANT_TOOLS: frozenset[str] = frozenset({"tools.invoke"})

# Volume: each name does NOT re-enter and has no inner recorder, so skipping it forgoes the
# only record that would ever exist. Taken deliberately for the usage plane alone, where a
# row is a per-call Postgres write on the highest-frequency agent call. Telemetry and denial
# audit do not skip this set — a span and its metric points stay in process (the span is
# sampled besides), and that is the plane where discovery latency is diagnosed.
UNMETERED_TOOLS: frozenset[str] = frozenset({"tools.search"})


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
