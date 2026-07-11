"""TDD tests for the ``tools.invoke`` gateway dispatcher (ADR-0268, #866).

Coverage:
* Dispatch to a real inner tool (session.whoami) returns the inner tool's response.
* An unknown tool name yields a ``configuration_error`` envelope with a pointer
  to ``tools.search`` in the detail.
* Missing required arguments for an inner tool yield ``configuration_error``.
* An inner tool's ``CategorizedError`` yields the same typed failure envelope as
  direct tool handlers.
* An inner tool that raises ``fastmcp.exceptions.AuthorizationError`` propagates
  unchanged — tools.invoke does not catch authorization errors (ADR-0148).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.app import build_app
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema_advertising import advertise_envelope_output_schema
from kdive.mcp.tools import gateway
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _verifier() -> JWTVerifier:
    kp = make_keypair()
    return JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)


def _viewer_ctx() -> RequestContext:
    return RequestContext(
        principal="viewer-user",
        agent_session="sess-viewer",
        projects=("proj-a",),
        roles={"proj-a": Role.VIEWER},
    )


def _call_result(result: Any) -> dict[str, Any]:
    """Extract the structured_content dict from an app.call_tool result."""
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict), f"expected structured_content dict, got {result!r}"
    return structured


# ---------------------------------------------------------------------------
# Test 1: successful dispatch to an inner tool
# ---------------------------------------------------------------------------


def test_invoke_dispatches_to_inner_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """tools.invoke passes the inner tool's successful response through unchanged."""
    import kdive.mcp.tools.identity.session as session_module

    monkeypatch.setattr(session_module, "current_context", _viewer_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.invoke", {"name": "session.whoami", "arguments": {}})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["data"]["principal"] == "viewer-user"
    assert content["status"] == "ok"


# ---------------------------------------------------------------------------
# Test 2: unknown tool name → configuration_error with tools.search pointer
# ---------------------------------------------------------------------------


def test_unknown_inner_name_is_configuration_error() -> None:
    """An unknown tool name returns a configuration_error pointing at tools.search."""
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.invoke", {"name": "no.such_tool", "arguments": {}})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["error_category"] == "configuration_error"
    assert "tools.search" in (content.get("detail") or "")


# ---------------------------------------------------------------------------
# Test 3: bad arguments for inner tool → configuration_error
# ---------------------------------------------------------------------------


def test_bad_arguments_is_configuration_error() -> None:
    """Missing required arguments for an inner tool yield configuration_error."""
    # runs.get requires run_id; passing {} triggers pydantic ValidationError
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.invoke", {"name": "runs.get", "arguments": {}})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["error_category"] == "configuration_error"
    # The detail should name the inner tool
    assert "runs.get" in (content.get("detail") or "")


# ---------------------------------------------------------------------------
# Test 4: inner CategorizedError is converted to an envelope
# ---------------------------------------------------------------------------


def test_inner_categorized_error_becomes_failure_envelope() -> None:
    """tools.invoke converts domain errors to the uniform failure envelope."""
    app = FastMCP("test-gateway-categorized-error")

    @app.tool(name="domain.fail")  # type: ignore[misc]
    async def _domain_fail() -> ToolResponse:
        raise CategorizedError(
            "store unavailable",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"operation": "capture"},
        )

    gateway.register(app, resolver=ProviderResolver({}))
    advertise_envelope_output_schema(app)

    async def _run() -> Any:
        return await app.call_tool("tools.invoke", {"name": "domain.fail", "arguments": {}})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["object_id"] == "tools.invoke"
    assert content["status"] == "error"
    assert content["error_category"] == "infrastructure_failure"
    assert content["detail"] == "store unavailable"
    assert content["data"] == {"operation": "capture"}


# ---------------------------------------------------------------------------
# Test 5: inner authorization error is not caught by tools.invoke
# ---------------------------------------------------------------------------


def test_inner_authorization_error_propagates() -> None:
    """tools.invoke does not catch AuthorizationError — it propagates unchanged.

    fastmcp.exceptions.AuthorizationError is a FastMCPError, so the fastmcp
    server re-raises it without wrapping (unlike non-FastMCPError exceptions,
    which become ToolError). tools.invoke must not add an except AuthorizationError
    clause — the caller (or outer middleware) handles it, exactly as a direct
    inner-tool call would (ADR-0148).
    """
    import fastmcp.exceptions as fmcp_exc

    app = FastMCP("test-gateway-auth")

    @app.tool(name="auth.gate")  # type: ignore[misc]
    async def _auth_gate() -> ToolResponse:
        raise fmcp_exc.AuthorizationError("not authorized")

    gateway.register(app, resolver=ProviderResolver({}))
    advertise_envelope_output_schema(app)

    async def _run() -> Any:
        return await app.call_tool("tools.invoke", {"name": "auth.gate", "arguments": {}})

    with pytest.raises(fmcp_exc.AuthorizationError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _secret_registry() -> Any:
    from kdive.security.secrets.secret_registry import SecretRegistry

    return SecretRegistry()
