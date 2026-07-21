"""Opt-in ASGI transport-trace middleware for MCP session/HTTP debugging (ADR-0417).

Neither of kdive's existing middleware layers observes the HTTP/transport layer where the
MCP session lifecycle, the ``initialize`` handshake, ``resources/*`` reads, and
transport-level status codes (e.g. ``404 Session not found``) happen: the FastMCP tool-call
middleware fires only for a dispatched tool call (above the transport), and the one existing
ASGI entry (`BareBearerHintMiddleware`, ADR-0380) is auth-only. This middleware, wired
outermost through `server_http_middleware()` when ``KDIVE_MCP_TRACE`` is set, logs one
structured line per HTTP request so an operator can reconstruct a session lifecycle
(initialize -> requests -> 404 -> re-initialize-or-not) from server logs alone (#1391).

The trace logger carries its own explicit ``INFO`` level so a trace line emits whenever the
flag is on, independent of the root floor ``KDIVE_LOG_LEVEL`` sets: the flag, not the global
level, is the gate. The line is emitted at ``http.response.start`` (the response headers) so
a long-lived SSE stream never withholds it; a ``finally`` emits the line only when
response-start was never seen (a pre-header failure). ``Authorization`` is logged as
presence only, never the value.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from kdive import config
from kdive.config.core_settings import MCP_TRACE

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_log = logging.getLogger("kdive.mcp.transport_trace")
_log.setLevel(logging.INFO)


def mcp_trace_enabled() -> bool:
    """Return whether opt-in transport tracing is enabled (``KDIVE_MCP_TRACE`` truthy)."""
    raw = config.get(MCP_TRACE)
    return bool(raw) and raw.strip().lower() in _TRUTHY


def _header(scope: Scope, name: bytes) -> str | None:
    """Return the value of header ``name`` (lowercase bytes) from an ASGI scope, or ``None``."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None


class TransportTraceMiddleware:
    """Log one structured line per HTTP request for MCP transport debugging (ADR-0417)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Trace one HTTP request; pass non-HTTP scopes straight through.

        Per-request state (``emitted``, timing, header fields) is local to this call and
        captured by the nested ``send`` wrapper via ``nonlocal`` — never stored on ``self``,
        which Starlette shares across every concurrent request.
        """
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        emitted = False
        session_id = _header(scope, b"mcp-session-id")
        fields: dict[str, Any] = {
            "method": scope.get("method", ""),
            "path": scope.get("path", ""),
            "mcp_session_id": session_id,
            "mcp_session_id_present": session_id is not None,
            "mcp_protocol_version": _header(scope, b"mcp-protocol-version"),
            "authorization_present": _header(scope, b"authorization") is not None,
        }

        def _emit(status: int | None) -> None:
            nonlocal emitted
            emitted = True
            duration_ms = (time.monotonic() - start) * 1000.0
            _log.info(
                "mcp transport %s %s -> %s",
                fields["method"],
                fields["path"],
                status,
                extra={**fields, "status": status, "duration_ms": duration_ms},
            )

        async def _wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start" and not emitted:
                _emit(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, _wrapped_send)
        finally:
            if not emitted:
                _emit(None)
