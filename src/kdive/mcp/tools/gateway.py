"""The tool gateway: tools.invoke (dispatcher) + tools.search (discovery) (ADR-0268, #866)."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from fastmcp.tools.base import ToolResult
from pydantic import Field, ValidationError

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta


def register(app: FastMCP) -> None:
    """Register the gateway tools (``tools.invoke``) on ``app``."""

    @app.tool(
        name="tools.invoke",
        annotations=_docmeta.destructive(),
        meta={"maturity": "implemented"},
    )
    async def tools_invoke(
        name: Annotated[
            str,
            Field(description="The registered tool to call (use tools.search to discover names)."),
        ],
        arguments: Annotated[
            dict[str, Any] | None,
            Field(description="Arguments object for that tool; omit or pass {} for no-arg tools."),
        ] = None,
    ) -> ToolResult:
        """Call any registered tool by name (gateway dispatch, ADR-0268).

        Re-enters the server's own dispatch path with ``run_middleware=True`` so the
        inner tool runs through the full middleware stack — RBAC, telemetry, binding
        validation, and denial audit — natively, exactly as a direct call would.

        ``AuthorizationError`` from the inner call is NOT caught here; the denial-audit
        middleware handles it and converts it to an ``authorization_denied`` envelope
        (ADR-0148). Only ``NotFoundError`` (unknown/disabled tool) and pydantic
        ``ValidationError`` (invalid arguments) are caught and converted to
        ``configuration_error`` envelopes.
        """
        try:
            return await app.call_tool(name, arguments or {}, run_middleware=True)
        except NotFoundError:
            envelope = ToolResponse.failure(
                "tools.invoke",
                ErrorCategory.CONFIGURATION_ERROR,
                detail=(
                    f"No tool named {name!r} is registered or enabled; "
                    "discover available tools with tools.search."
                ),
            )
            return ToolResult(structured_content=envelope.model_dump(mode="json"))
        except ValidationError:
            envelope = ToolResponse.failure(
                "tools.invoke",
                ErrorCategory.CONFIGURATION_ERROR,
                detail=f"Arguments for {name!r} failed schema validation.",
            )
            return ToolResult(structured_content=envelope.model_dump(mode="json"))
