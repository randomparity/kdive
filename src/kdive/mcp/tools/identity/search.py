"""``tools.search`` — capability discovery over the full tool surface (ADR-0267, #866).

The gateway hides the long tail of tools from ``list_tools`` (the core-set tier filter), so an
agent needs a way to reach a demoted tool. ``tools.search`` maps a capability phrase to the
matching tools and returns each one's **full input schema** — the same payload ``list_tools``
would have emitted — so the result is sufficient to *construct a call*, not just a name hint. The
agent then calls the returned tool directly by name (the 1a client model; no ``list_tools``
injection).

It is PUBLIC: any authenticated caller may search, but results are filtered to the tools the
caller could actually invoke under its grants (:func:`kdive.mcp.exposure.visible_tool_names`),
across **all** tiers — search is the escape hatch out of the core set. Ranking is the
deterministic lexical scorer in :mod:`kdive.mcp.tool_index` (no embeddings, ADR-0267).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.exposure import visible_tool_names
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.schema_advertising import registered_tools
from kdive.mcp.tool_index import rank_tools, render_instructions
from kdive.mcp.tools import _docmeta
from kdive.security.authz.context import RequestContext

_log = logging.getLogger(__name__)

#: Default and hard cap on returned matches. A small cap keeps a vague broad-matching query from
#: re-dumping a large slice of the catalog — the cost the gateway exists to remove (ADR-0267).
SEARCH_LIMIT_DEFAULT = 8
SEARCH_LIMIT_MAX = 20


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """One registered tool as search sees it: name, description, and its input schema."""

    name: str
    description: str
    input_schema: dict[str, JsonValue]


def search_tools(
    catalog: list[ToolEntry], ctx: RequestContext, query: str, limit: int
) -> ToolResponse:
    """Rank ``catalog`` against ``query`` for ``ctx`` and return constructible tool schemas.

    An empty/whitespace query is a ``configuration_error`` (``reason=empty_query``) pointing at
    the namespace table of contents. Otherwise the catalog is RBAC-filtered to what ``ctx`` may
    invoke (all tiers), ranked, and capped at ``limit``; each result carries the tool's full
    input schema so the caller can construct a call. A zero-result query is a success with an
    empty list and a structured log (the search-miss signal).
    """
    if not query.strip():
        return ToolResponse.failure(
            ctx.principal,
            ErrorCategory.CONFIGURATION_ERROR,
            detail="search query is empty; describe the capability you need",
            data={"reason": "empty_query", "namespaces": render_instructions()},
        )
    by_name = {entry.name: entry for entry in catalog}
    visible = visible_tool_names(ctx, by_name.keys())
    candidates = [(e.name, e.description) for e in catalog if e.name in visible]
    ranked = rank_tools(query, candidates, limit=limit)
    if not ranked:
        _log.info("tools.search miss", extra={"query": query, "result_count": 0})
    results: list[JsonValue] = [
        {
            "name": name,
            "description": by_name[name].description,
            "input_schema": by_name[name].input_schema,
        }
        for name in ranked
    ]
    return ToolResponse.success(
        ctx.principal,
        "ok",
        data={"results": results, "result_count": len(results)},
    )


def _catalog(app: FastMCP) -> list[ToolEntry]:
    """Snapshot the live registry as search entries (raw store, not the filtered list_tools)."""
    return [
        ToolEntry(tool.name, tool.description or "", dict(tool.parameters))
        for tool in registered_tools(app)
    ]


def register(app: FastMCP, _pool: AsyncConnectionPool) -> None:
    """Register ``tools.search`` (PUBLIC; closes over ``app`` to read the live registry)."""

    @app.tool(
        name="tools.search",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def tools_search(
        query: Annotated[
            str,
            Field(description="Capability phrase, e.g. 'boot a kernel' or 'read guest memory'."),
        ],
        limit: Annotated[
            int,
            Field(
                description="Max matches to return.",
                ge=1,
                le=SEARCH_LIMIT_MAX,
            ),
        ] = SEARCH_LIMIT_DEFAULT,
    ) -> ToolResponse:
        """Find tools by capability and return their full schemas to call directly by name."""
        return search_tools(_catalog(app), current_context(), query, limit)
