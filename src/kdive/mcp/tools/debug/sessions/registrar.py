"""FastMCP registration for debug session and debug-op tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.capacity.state import DebugSessionState
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT as _DEFAULT_LIST_LIMIT
from kdive.mcp.tools._common import MAX_LIST_LIMIT as _MAX_LIST_LIMIT
from kdive.mcp.tools.debug.sessions.lifecycle import (
    _GDBSTUB,
    DebugSessionHandlers,
)
from kdive.mcp.tools.debug.sessions.read import SessionsListRequest as _SessionsListRequest
from kdive.mcp.tools.debug.sessions.read import get_session as _get_session
from kdive.mcp.tools.debug.sessions.read import list_sessions as _list_sessions
from kdive.observability.debug_session_telemetry import DebugSessionTelemetry
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry


class _DebugSessionsListPayload(ToolPayload):
    """Public payload for ``debug.list_sessions`` filters."""

    run_id: str | None = Field(default=None, description="Only sessions for this Run id.")
    system_id: str | None = Field(default=None, description="Only sessions on this System id.")
    project: str | None = Field(
        default=None,
        description="Only sessions in this project (within your membership).",
    )
    state: DebugSessionState | None = Field(
        default=None,
        description="Only sessions in this lifecycle state.",
    )
    limit: int = Field(
        default=_DEFAULT_LIST_LIMIT,
        description=f"Maximum rows returned (capped at {_MAX_LIST_LIMIT}).",
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
    )


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    telemetry: DebugSessionTelemetry | None = None,
) -> None:
    """Register the ``debug.*`` tools on ``app``, bound to ``pool``."""
    from kdive.mcp.tools.debug.operations.registrar import _register_debug_ops
    from kdive.mcp.tools.debug.operations.runtime import DebugRuntimeResolver

    runtime = DebugRuntimeResolver(resolver)
    handlers = DebugSessionHandlers.from_resolver(
        resolver,
        runtime_resolver=runtime,
        secret_registry=secret_registry,
        telemetry=telemetry,
    )

    @app.tool(
        name="debug.start_session",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def debug_start_session(
        run_id: Annotated[str, Field(description="The booted Run to attach a debug session to.")],
        transport: Annotated[
            str,
            Field(description="Transport kind: `gdbstub` (default) or `drgn-live`."),
        ] = _GDBSTUB,
    ) -> ToolResponse:
        """Open a single-attach transport and insert a live DebugSession. Requires contributor."""
        return await handlers.start_session(
            pool, current_context(), run_id=run_id, transport=transport
        )

    @app.tool(
        name="debug.end_session",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def debug_end_session(
        session_id: Annotated[str, Field(description="The DebugSession to detach and close.")],
    ) -> ToolResponse:
        """Drive a live DebugSession to detached; close its transport. Requires contributor."""
        return await handlers.end_session(pool, current_context(), session_id)

    @app.tool(
        name="debug.get_session",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def debug_get_session(
        session_id: Annotated[str, Field(description="The DebugSession to inspect.")],
    ) -> ToolResponse:
        """Return one visible debug session for recovery. Requires viewer."""
        return await _get_session(pool, current_context(), session_id)

    @app.tool(
        name="debug.list_sessions",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def debug_list_sessions(
        request: Annotated[
            _DebugSessionsListPayload | None,
            Field(description="Debug session list filters."),
        ] = None,
    ) -> ToolResponse:
        """List the caller's debug sessions, filterable by run/system/project/state. Viewer."""
        payload = request or _DebugSessionsListPayload()
        list_request = _SessionsListRequest(
            run_id=payload.run_id,
            system_id=payload.system_id,
            project=payload.project,
            state=payload.state.value if payload.state is not None else None,
            limit=payload.limit,
            cursor=payload.cursor,
        )
        return await _list_sessions(pool, current_context(), list_request)

    _register_debug_ops(app, pool, runtime)
