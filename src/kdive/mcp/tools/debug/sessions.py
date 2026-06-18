"""FastMCP registration for debug session and debug-op tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.debug.ops import DebugRuntimeResolver, _register_debug_ops
from kdive.mcp.tools.debug.sessions_lifecycle import (
    _GDBSTUB,
    _AttachRequest,
    _insert_session_locked,
    _InsertSession,
    _resolved_connector_for_run,
    _resolved_detach_resources,
    _secret_scope,
)
from kdive.mcp.tools.debug.sessions_lifecycle import (
    DebugSessionHandlers as _LifecycleDebugSessionHandlers,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

__all__ = [
    "DebugSessionHandlers",
    "_AttachRequest",
    "_GDBSTUB",
    "_insert_session_locked",
    "register",
]


class DebugSessionHandlers(_LifecycleDebugSessionHandlers):
    """Registrar-facing lifecycle handler preserving the historical test seam."""

    @classmethod
    def from_resolver(
        cls,
        resolver: ProviderResolver,
        *,
        runtime_resolver: DebugRuntimeResolver | None,
        insert_session_locked: _InsertSession | None = None,
        secret_backend_factory: Callable[[UUID], SecretBackend] | None = None,
        secret_registry: SecretRegistry,
    ) -> DebugSessionHandlers:
        return cls(
            connector_for_run=_resolved_connector_for_run(resolver),
            detach_resources=_resolved_detach_resources(resolver, runtime_resolver),
            insert_session_locked=(
                _insert_session_locked if insert_session_locked is None else insert_session_locked
            ),
            secret_backend_factory=secret_backend_factory,
            secret_registry=secret_registry,
        )


def _secret_backend_factory(secret_registry: SecretRegistry):
    def _factory(session_id: UUID):
        return secret_backend_from_env(
            registry=secret_registry,
            scope=_secret_scope(session_id),
        )

    return _factory


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the ``debug.*`` tools on ``app``, bound to ``pool``."""
    runtime = DebugRuntimeResolver(resolver)
    handlers = DebugSessionHandlers.from_resolver(
        resolver,
        runtime_resolver=runtime,
        secret_backend_factory=_secret_backend_factory(secret_registry),
        secret_registry=secret_registry,
    )

    @app.tool(
        name="debug.start_session",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Opens a single-attach gdbstub/drgn-live transport to a booted Run; requires "
                "a real booted kernel, reached only under the gated live markers."
            ),
            promotion=(
                "A non-gated test or recorded live_stack run attaches a debug session to a "
                "real booted Run."
            ),
            providers="local-libvirt: wired; remote-libvirt: wired; fault-inject: n/a.",
        ),
    )
    async def debug_start_session(
        run_id: Annotated[str, Field(description="The booted Run to attach a debug session to.")],
        transport: Annotated[
            str,
            Field(description="Transport kind: `gdbstub` (default) or `drgn-live`."),
        ] = _GDBSTUB,
    ) -> ToolResponse:
        """Open a single-attach transport and insert a live DebugSession. Requires operator."""
        return await handlers.start_session(
            pool, current_context(), run_id=run_id, transport=transport
        )

    @app.tool(
        name="debug.end_session",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Detaches and closes a live DebugSession's transport; requires a real attached "
                "session, reached only under the gated live markers."
            ),
            promotion=(
                "A non-gated test or recorded live_stack run ends a debug session attached to "
                "a real booted Run."
            ),
            providers="local-libvirt: wired; remote-libvirt: wired; fault-inject: n/a.",
        ),
    )
    async def debug_end_session(
        session_id: Annotated[str, Field(description="The DebugSession to detach and close.")],
    ) -> ToolResponse:
        """Drive a live/attach DebugSession to detached; close its transport. Requires operator."""
        return await handlers.end_session(pool, current_context(), session_id)

    _register_debug_ops(app, pool, runtime)
