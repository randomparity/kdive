"""End-to-end: the transport trace runs outermost over the real FastMCP http_app (ADR-0417).

Drives the real assembled app in-process (no live server, no DB — the pool is unopened and
the auth/session layers reject before any DB access, mirroring
``tests/mcp/core/test_bare_bearer_ordering.py``), proving the trace observes both a
peer-middleware 401 and FastMCP's vendored transport 404 session-miss — the visibility #1391
exists to provide.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.mcp.middleware.transport_trace import TransportTraceMiddleware
from kdive.processes.server import server_http_middleware
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair, mint

TRACE_LOGGER = "kdive.mcp.transport_trace"
_MCP_PATH = "/mcp"


class _CaptureHandler(logging.Handler):
    """Collect emitted records for assertion (avoids caplog's root-propagation quirks)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[Any] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _post(headers: dict[str, str], *, valid_auth: bool = False) -> tuple[httpx.Response, list[Any]]:
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    http_app = app.http_app(middleware=server_http_middleware(trace_enabled=True))
    sent = dict(headers)
    if valid_auth:  # a token the verifier trusts, so auth passes and the session layer runs
        sent["Authorization"] = f"Bearer {mint(kp)}"

    transport = httpx.ASGITransport(app=http_app)

    async def _run() -> httpx.Response:
        # Run the Starlette lifespan so FastMCP's streamable-HTTP session manager task group
        # is initialized; without it a session lookup raises before returning its 404.
        async with (
            http_app.router.lifespan_context(http_app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test", follow_redirects=True
            ) as client,
        ):
            return await client.post(
                _MCP_PATH,
                headers={"Accept": "application/json, text/event-stream", **sent},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )

    handler = _CaptureHandler()
    trace_log = logging.getLogger(TRACE_LOGGER)
    trace_log.addHandler(handler)
    try:
        resp = asyncio.run(_run())
    finally:
        trace_log.removeHandler(handler)
    return resp, [r for r in handler.records if r.name == TRACE_LOGGER]


def test_trace_observes_bare_bearer_401() -> None:
    resp, records = _post({"Authorization": mint(make_keypair())})  # bare JWT → 401
    assert resp.status_code == 401
    assert any(r.status == 401 for r in records)


def test_trace_observes_transport_session_miss_404() -> None:
    resp, records = _post({"Mcp-Session-Id": "does-not-exist"}, valid_auth=True)
    assert resp.status_code == 404
    assert any(r.status == 404 for r in records)


def test_seam_places_trace_outermost_in_real_stack() -> None:
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    http_app = app.http_app(middleware=server_http_middleware(trace_enabled=True))
    user_mw = getattr(http_app, "user_middleware", [])
    assert any(getattr(m, "cls", None) is TransportTraceMiddleware for m in user_mw)
