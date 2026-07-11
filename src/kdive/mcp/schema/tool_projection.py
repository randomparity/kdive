"""Provider-kind schema projection for advertised tools."""

from __future__ import annotations

from fastmcp.tools import Tool

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.schema.provider_schema import project_tool_schema

#: Tools whose published ``inputSchema`` is narrowed to the composed ``ResourceKind`` set.
NARROWED_TOOLS: frozenset[str] = frozenset(
    {"allocations.request", "systems.define", "systems.provision", "systems.reprovision"}
)


def project_listed_tool(tool: Tool, kinds: frozenset[ResourceKind]) -> Tool:
    """Return ``tool`` with its inputSchema narrowed to ``kinds``, or unchanged."""
    if tool.name not in NARROWED_TOOLS:
        return tool
    projected = project_tool_schema(tool.parameters, kinds)
    return tool.model_copy(update={"parameters": projected})
