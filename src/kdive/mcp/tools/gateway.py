"""The tool gateway: tools.invoke (dispatcher) + tools.search (discovery) (ADR-0268, #866).

The inner-call denial path (``authorization_denied`` via the denial-audit middleware) is
ADR-0148. Agent-rendered docstrings here carry no ADR citation (ADR-0270).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, cast

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from fastmcp.tools.base import Tool, ToolResult
from pydantic import Field, ValidationError

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.exposure import tool_visible
from kdive.mcp.middleware.exposure import project_listed_tool
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema_advertising import registered_tools
from kdive.mcp.tool_index import TOOL_KEYWORDS
from kdive.mcp.tools import _docmeta
from kdive.providers.core.resolver import ProviderResolver
from kdive.serialization import JsonValue

_log = logging.getLogger(__name__)

# Hard cap on search results — prevents one broad query from re-emitting the full catalog.
_SEARCH_LIMIT_MAX = 50


def _score(tool: Tool, tokens: list[str]) -> int:
    """Lexical score: count of query tokens found as substrings in the tool's haystack."""
    extras = TOOL_KEYWORDS.get(tool.name, frozenset())
    haystack = " ".join([tool.name, tool.description or "", *extras]).lower()
    return sum(1 for tok in tokens if tok in haystack)


def _rank(candidates: list[Tool], *, query: str | None, namespace: str | None) -> list[Tool]:
    """Return the ordered candidate list for the given search mode.

    - Namespace mode: filter by ``"<namespace>."`` prefix, sort lexicographically.
    - Query mode: score by substring hits in name + description + curated keywords,
      keep only tools with score > 0, sort by (score DESC, name ASC).
    - Fallback (neither): all candidates sorted by name.
    """
    if namespace is not None:
        prefix = f"{namespace}."
        return sorted(
            (t for t in candidates if t.name.startswith(prefix)),
            key=lambda t: t.name,
        )
    if query is not None:
        tokens = [tok for tok in query.lower().split() if len(tok) >= 2]
        if not tokens:
            return []
        scored = [(t, _score(t, tokens)) for t in candidates]
        hits = [(t, s) for t, s in scored if s > 0]
        hits.sort(key=lambda x: (-x[1], x[0].name))
        return [t for t, _ in hits]
    return sorted(candidates, key=lambda t: t.name)


def describe_tool(tool: Tool, kinds: frozenset[ResourceKind]) -> dict[str, JsonValue]:
    """Serialise a Tool into the ``{name, description, input_schema}`` match shape,
    narrowing the input schema to the composed ``kinds`` (ADR-0269)."""
    return cast(
        "dict[str, JsonValue]",
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": project_listed_tool(tool, kinds).parameters,
        },
    )


def register(app: FastMCP, *, resolver: ProviderResolver) -> None:
    """Register the gateway tools (``tools.invoke``, ``tools.search``) on ``app``.

    Args:
        app: The FastMCP application to register tools on.
        resolver: Provider resolver used to narrow tool schemas in search results.
    """

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
        """Call any registered tool by name (gateway dispatch).

        Re-enters the server's own dispatch path with ``run_middleware=True`` so the
        inner tool runs through the full middleware stack — RBAC, telemetry, binding
        validation, and denial audit — natively, exactly as a direct call would.

        ``AuthorizationError`` from the inner call is NOT caught here; the denial-audit
        middleware handles it and converts it to an ``authorization_denied`` envelope.
        Only ``NotFoundError`` (unknown/disabled tool) and pydantic ``ValidationError``
        (invalid arguments) are caught and converted to ``configuration_error`` envelopes.
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

    @app.tool(
        name="tools.search",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def tools_search(
        query: Annotated[
            str | None,
            Field(description="Capability phrase to search for (e.g. 'boot a built kernel')."),
        ] = None,
        namespace: Annotated[
            str | None,
            Field(description="Browse one tool plane by prefix, e.g. 'debug' or 'runs'."),
        ] = None,
        limit: Annotated[
            int,
            Field(ge=1, le=_SEARCH_LIMIT_MAX, description="Maximum matches to return (1-50)."),
        ] = 10,
    ) -> ToolResponse:
        """Find tools by capability phrase or namespace; returns full schemas for tools.invoke.

        Two modes:
        - ``query``: lexical ranking over name + description + curated keywords; returns tools
          matching the query, highest-scoring first.
        - ``namespace``: enumerate all tools in one plane (e.g. ``"debug"``); returns them
          sorted by name. Use this as a safety net when a query misses.

        Results are RBAC-filtered to only tools the caller could invoke. Each match carries
        ``name``, ``description``, and ``input_schema`` so you can immediately call
        ``tools.invoke`` with the right arguments.

        ``truncated: true`` signals that more results exist beyond the returned ``limit``.
        When ``query`` produces zero results, the miss is logged for keyword curation.
        """
        ctx = current_context()
        all_tools = list(registered_tools(app))
        candidates = [t for t in all_tools if tool_visible(t.name, ctx)]
        ranked = _rank(candidates, query=query, namespace=namespace)
        matches = ranked[:limit]
        if not matches and query is not None:
            _log.info(
                "tool_search_miss",
                extra={"query": query, "count": 0},
            )
        kinds = resolver.registered_kinds()
        return ToolResponse.success(
            "tools.search",
            "ok",
            data={
                "matches": cast("JsonValue", [describe_tool(t, kinds) for t in matches]),
                "truncated": len(ranked) > limit,
            },
        )
