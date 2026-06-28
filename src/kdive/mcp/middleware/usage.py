"""Per-call usage recording and outcome classification."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import Middleware

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.shared import (
    META_TOOLS,
    ToolOutcome,
    request_context,
    result_error_category,
)
from kdive.mcp.tools._platform_auth import actor_for
from kdive.security.authz.rbac import AuthorizationError
from kdive.security.usage import UsageEvent, record_usage

_log = logging.getLogger(__name__)


def _call_project(context: Any) -> str | None:
    """The call's ``project`` argument, if present as a non-empty string."""
    arguments = getattr(context.message, "arguments", None)
    if isinstance(arguments, dict):
        value = arguments.get("project")
        if isinstance(value, str) and value:
            return value
    return None


class UsageTrackingMiddleware(Middleware):
    """Record one best-effort ``tool_invocation`` row per call."""

    def __init__(self, pool: Any, *, acquire_timeout: float = 1.0) -> None:
        self._pool = pool
        self._acquire_timeout = acquire_timeout

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one call, then record its outcome best-effort."""
        if getattr(context.message, "name", "?") in META_TOOLS:
            return await call_next(context)
        try:
            result = await call_next(context)
        except AuthorizationError:
            await self._record(context, ToolOutcome.DENIED)
            raise
        except Exception:
            await self._record(context, ToolOutcome.ERROR)
            raise
        await self._record(context, self._classify(result))
        return result

    @staticmethod
    def _classify(result: Any) -> ToolOutcome:
        category = result_error_category(result)
        if category is None:
            return ToolOutcome.OK
        if category == ErrorCategory.AUTHORIZATION_DENIED:
            return ToolOutcome.DENIED
        return ToolOutcome.ERROR

    async def _record(self, context: Any, outcome: ToolOutcome) -> None:
        tool = getattr(context.message, "name", "?")
        try:
            ctx = request_context()
            event = UsageEvent(
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=_call_project(context),
                tool=tool,
                outcome=outcome.value,
                actor=actor_for(ctx),
                client_id=ctx.client_id,
            )
            async with (
                self._pool.connection(timeout=self._acquire_timeout) as conn,
                conn.transaction(),
            ):
                await record_usage(conn, event)
        except Exception:
            _log.warning("usage recording failed for tool %s", tool, exc_info=True)
