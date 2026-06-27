"""Advertise the uniform ToolResponse output schema for registered MCP tools."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool

# A fielded, non-recursive output schema advertised for every tool (ADR-0170, revisiting
# ADR-0113). Every tool returns the self-referential `ToolResponse` (`items: list[ToolResponse]` +
# recursive `JsonValue` data), so FastMCP would auto-derive a recursive `$ref` schema that the
# FastMCP 3.4.0 client cannot build a validator for. This schema documents every top-level
# envelope field while collapsing recursive fields.
ENVELOPE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The uniform kdive ToolResponse envelope (ADR-0019). `data` and `items` are "
        "intentionally open; see resource://kdive/docs/guide/response-envelope.md."
    ),
    "properties": {
        "object_id": {"type": "string"},
        "status": {"type": "string"},
        "suggested_next_actions": {"type": "array", "items": {"type": "string"}},
        "refs": {"type": "object", "additionalProperties": {"type": "string"}},
        "error_category": {"type": ["string", "null"]},
        "retryable": {"type": ["boolean", "null"]},
        "detail": {"type": ["string", "null"]},
        "data": {"type": "object"},
        "items": {"type": "array", "items": {"type": "object"}},
    },
}


def registered_tools(app: FastMCP) -> Iterator[Tool]:
    """Yield each registered `Tool` from the local provider's component store."""
    for component in app.local_provider._components.values():
        if isinstance(component, Tool):
            yield component


def advertise_envelope_output_schema(app: FastMCP) -> int:
    """Override every registered tool's advertised `outputSchema` with the envelope schema."""
    swept = 0
    for tool in registered_tools(app):
        tool.output_schema = dict(ENVELOPE_OUTPUT_SCHEMA)
        swept += 1
    if swept == 0:
        raise RuntimeError(
            "no tools found to advertise an envelope outputSchema for; the FastMCP registry "
            "accessor (app.local_provider._components) may have changed (ADR-0170)"
        )
    return swept
