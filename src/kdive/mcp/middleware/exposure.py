"""Per-connection tool exposure filtering middleware."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool

import kdive.config as config
from kdive.config.core_settings import MCP_TOOL_GATEWAY
from kdive.mcp.exposure import CORE_TOOLS, visible_tool_names
from kdive.mcp.middleware.shared import request_context
from kdive.security.authz.errors import AuthError

_log = logging.getLogger(__name__)


def _gateway_enabled() -> bool:
    """Return True when KDIVE_MCP_TOOL_GATEWAY is set to on/1/true (default off)."""
    return (config.get(MCP_TOOL_GATEWAY) or "").strip().lower() in {"on", "1", "true"}


class ToolExposureMiddleware(Middleware):
    """Filter ``list_tools`` to tools the connection's grants could invoke."""

    async def on_list_tools(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Sequence[Tool]:
        """Return only the advertised tools the in-flight connection may invoke."""
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
        return [tool for tool in tools if tool.name in visible]
