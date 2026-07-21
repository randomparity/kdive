"""End-to-end ordering: the bare-JWT hint middleware sits ahead of vendored auth (ADR-0380).

Drives the real FastMCP `http_app` (with kdive's `server_http_middleware()`) through an
in-process ASGI transport to prove the middleware short-circuits a bare token *before*
FastMCP's `RequireAuthMiddleware` endpoint wrapper, while a scheme-prefixed token still
reaches the vendored verifier and its normal error.
"""

from __future__ import annotations

import asyncio

import httpx
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.processes.server import server_http_middleware
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair, mint

_MCP_PATH = "/mcp"


def _post(headers: dict[str, str]) -> httpx.Response:
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    http_app = app.http_app(middleware=server_http_middleware(trace_enabled=False))

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=http_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=True
        ) as client:
            return await client.post(
                _MCP_PATH,
                headers={"Accept": "application/json, text/event-stream", **headers},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )

    return asyncio.run(_run())


def _valid_bare_token() -> str:
    return mint(make_keypair())  # a well-formed JWT; sent without the Bearer prefix


def test_bare_jwt_gets_scheme_hint_not_vendored_error() -> None:
    resp = _post({"Authorization": _valid_bare_token()})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "invalid_request"
    assert "Bearer " in body["error_description"]
    # The vendored misdirection must not appear.
    assert "invalid, expired" not in body["error_description"]


def test_invalid_bearer_token_still_gets_vendored_error() -> None:
    # A scheme-prefixed but bogus token passes our middleware and reaches vendored auth.
    resp = _post({"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    body = resp.json()
    # Vendored FastMCP RequireAuthMiddleware wording — proves we did not shadow it.
    assert "invalid, expired" in body["error_description"]


def test_missing_authorization_still_gets_vendored_error() -> None:
    resp = _post({})
    assert resp.status_code == 401
    # No Authorization at all → vendored auth path, not our bare-token hint. FastMCP's
    # RequireAuthMiddleware answers a wholly missing credential with a bodyless 401 carrying a
    # `WWW-Authenticate: Bearer` challenge, unlike our hint's `invalid_request` JSON body.
    assert resp.headers.get("www-authenticate", "").startswith("Bearer")
    assert "invalid_request" not in resp.text
