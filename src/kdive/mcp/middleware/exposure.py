"""Per-connection tool exposure filtering middleware."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool
from opentelemetry import metrics

import kdive.config as config
from kdive.config.core_settings import MCP_TOOL_GATEWAY
from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.exposure import CORE_TOOLS, visible_tool_names
from kdive.mcp.middleware.shared import request_context
from kdive.mcp.provider_schema import project_tool_schema
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.errors import AuthError

_log = logging.getLogger(__name__)

_PROJECTION_FAILURES = metrics.get_meter("kdive.mcp").create_counter(
    "kdive_mcp_provider_schema_projection_failures",
    description="provider-schema projection fell open to the full schema (ADR-0269)",
)

#: Tools whose published ``inputSchema`` is narrowed to the composed ``ResourceKind`` set.
NARROWED_TOOLS: frozenset[str] = frozenset(
    {"allocations.request", "systems.define", "systems.provision"}
)


def project_listed_tool(tool: Tool, kinds: frozenset[ResourceKind]) -> Tool:
    """Return ``tool`` with its inputSchema narrowed to ``kinds`` (or unchanged).

    Args:
        tool: A FastMCP ``Tool`` instance from the live registry.
        kinds: The frozenset of currently composed ``ResourceKind`` values.

    Returns:
        A new ``Tool`` (via ``model_copy``) with narrowed parameters for tools in
        ``NARROWED_TOOLS``, or the original ``tool`` object for unaffected tools.
    """
    if tool.name not in NARROWED_TOOLS:
        return tool
    projected = project_tool_schema(tool.parameters, kinds)
    return tool.model_copy(update={"parameters": projected})


def _gateway_enabled() -> bool:
    """Return True when KDIVE_MCP_TOOL_GATEWAY is set to on/1/true (default off)."""
    return (config.get(MCP_TOOL_GATEWAY) or "").strip().lower() in {"on", "1", "true"}


class ToolExposureMiddleware(Middleware):
    """Filter ``list_tools`` to tools the connection's grants could invoke.

    Also narrows each affected tool's ``inputSchema`` to the composed provider kinds
    (ADR-0269). The projection is fail-open: a schema narrowing error advertises the full
    schema, increments a counter, and logs a warning so a silent revert is observable.
    """

    def __init__(self, resolver: ProviderResolver) -> None:
        self._resolver = resolver

    async def on_list_tools(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Sequence[Tool]:
        """Return only the advertised tools the in-flight connection may invoke.

        Narrows each narrowed tool's ``inputSchema`` to the composed provider kinds after
        the RBAC filter has reduced the catalog to the visible set.
        """
        tools: Sequence[Tool] = await call_next(context)
        try:
            ctx = request_context()
            visible = visible_tool_names(ctx, (tool.name for tool in tools))
            if _gateway_enabled():
                visible &= CORE_TOOLS
        except AuthError:
            _log.debug("no verified token in on_list_tools; advertising the full catalog")
            return tools
        except Exception:
            _log.warning("tool-exposure filter failed; advertising the full catalog", exc_info=True)
            return tools
        kinds = self._resolver.registered_kinds()
        result: list[Tool] = []
        for tool in tools:
            if tool.name not in visible:
                continue
            try:
                result.append(project_listed_tool(tool, kinds))
            except Exception:
                _PROJECTION_FAILURES.add(1)
                _log.warning(
                    "provider-schema projection failed for %s; advertising full schema",
                    tool.name,
                    exc_info=True,
                )
                result.append(tool)
        return result
