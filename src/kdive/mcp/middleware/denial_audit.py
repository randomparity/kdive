"""Authorization-denial audit middleware (ADR-0062, ADR-0098)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from math import isfinite
from typing import Any

from fastmcp.server.middleware import Middleware
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.shared import META_TOOLS, request_context
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.authz.errors import ProjectMembershipDenied
from kdive.security.authz.rbac import RoleDenied

_log = logging.getLogger(__name__)
_DROP_ARGUMENT = object()


def _current_agent_session() -> str | None:
    """Read the in-flight request's ``agent_session`` from the verified token."""
    return request_context().agent_session


def _json_argument(value: object) -> object:
    """Return a JSON-native copy of ``value``, or ``_DROP_ARGUMENT`` if it is not safe."""
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else _DROP_ARGUMENT
    if isinstance(value, list):
        values: list[object] = []
        for item in value:
            sanitized = _json_argument(item)
            if sanitized is _DROP_ARGUMENT:
                return _DROP_ARGUMENT
            values.append(sanitized)
        return values
    if isinstance(value, dict):
        values: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return _DROP_ARGUMENT
            sanitized = _json_argument(item)
            if sanitized is _DROP_ARGUMENT:
                return _DROP_ARGUMENT
            values[key] = sanitized
        return values
    return _DROP_ARGUMENT


def _audit_args_from_message(message: Any) -> dict[str, object]:
    """Extract the JSON-native MCP call arguments for denial-audit digesting."""
    raw = getattr(message, "arguments", None)
    if not isinstance(raw, dict):
        return {}
    args: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        sanitized = _json_argument(value)
        if sanitized is not _DROP_ARGUMENT:
            args[key] = sanitized
    return args


class DenialAuditMiddleware(Middleware):
    """Catch member-over-reach ``RoleDenied`` at the dispatch boundary and audit it."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        agent_session: Callable[[], str | None] = _current_agent_session,
    ) -> None:
        self._pool = pool
        self._agent_session = agent_session

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one tool call and map authorization denials to envelopes."""
        try:
            return await call_next(context)
        except RoleDenied as denial:
            tool = context.message.name
            args = _audit_args_from_message(context.message)
            try:
                await self._record(tool, denial, args=args)
            except Exception:
                _log.warning("failed to audit RoleDenied for tool %s", tool, exc_info=True)
            return ToolResponse.failure(tool, ErrorCategory.AUTHORIZATION_DENIED)
        except ProjectMembershipDenied:
            return ToolResponse.failure(context.message.name, ErrorCategory.AUTHORIZATION_DENIED)

    async def _record(
        self, tool: str, denial: RoleDenied, *, args: dict[str, object] | None = None
    ) -> None:
        if tool in META_TOOLS:
            return
        async with self._pool.connection() as conn, conn.transaction():
            await audit.record_denial(
                conn,
                event=audit.DenialEvent(
                    principal=denial.principal,
                    agent_session=self._agent_session(),
                    project=denial.project,
                    tool=tool,
                    args={} if args is None else args,
                    reason=str(denial),
                ),
            )
