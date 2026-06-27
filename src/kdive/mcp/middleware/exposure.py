"""Per-connection tool exposure filtering middleware."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool

from kdive.mcp.exposure import core_visible_tool_names, visible_tool_names
from kdive.mcp.middleware.shared import request_context
from kdive.security.authz.errors import AuthError

_log = logging.getLogger(__name__)


class ToolExposureMiddleware(Middleware):
    """Filter ``list_tools`` to tools the connection's grants could invoke.

    When ``gateway_enabled`` (ADR-0267), the result is further intersected with the core set so
    the default catalog is small; the long tail stays reachable via ``tools.search``. Advisory
    and fail-open: a missing context or any error returns the unfiltered catalog.
    """

    def __init__(self, *, gateway_enabled: bool = False) -> None:
        self._gateway_enabled = gateway_enabled

    async def on_list_tools(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Sequence[Tool]:
        """Return only the advertised tools the in-flight connection may invoke."""
        tools: Sequence[Tool] = await call_next(context)
        filter_names = core_visible_tool_names if self._gateway_enabled else visible_tool_names
        try:
            ctx = request_context()
            visible = filter_names(ctx, (tool.name for tool in tools))
        except AuthError:
            _log.debug("no verified token in on_list_tools; advertising the full catalog")
            return tools
        except Exception:
            _log.warning("tool-exposure filter failed; advertising the full catalog", exc_info=True)
            return tools
        return [tool for tool in tools if tool.name in visible]
