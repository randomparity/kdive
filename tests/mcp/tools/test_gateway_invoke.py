"""TDD tests for the ``tools.invoke`` gateway dispatcher (ADR-0268, #866).

Coverage:
* Dispatch to a real inner tool (session.whoami) returns the inner tool's response.
* An unknown tool name yields a ``configuration_error`` envelope with a pointer
  to ``tools.search`` in the detail.
* Missing required arguments for an inner tool yield ``configuration_error`` whose
  ``data`` names the missing field, its failure kind, and the tool's accepted
  top-level keys, with ``tools.search`` in ``suggested_next_actions`` (#2304).
* An unexpected keyword argument yields the same informative ``data`` shape,
  proving the parity holds across pydantic failure kinds, not just "missing" (#2304).
* ``accepted_fields`` and ``field_errors`` are both withheld from a caller with no verified
  context and from an authenticated caller who lacks the tool's required scope, matching
  ``tools.search``'s RBAC gate: argument binding fails before a handler's own ``require_role``
  check runs, so ``field_errors`` would otherwise leak a hidden tool's parameter names (#2304,
  #2325).
* ``field_errors`` stays capped under an adversarial argument count (#2304).
* A bare ``pydantic.ValidationError`` escaping a tool's own body (not an argument-binding
  failure) gets the pre-existing generic envelope, never fabricated field/schema detail
  attributed to the caller (#2304).
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
from kdive.mcp.assembly.app import build_app
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.schema_advertising import advertise_envelope_output_schema
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


def _no_grant_ctx() -> RequestContext:
    """An authenticated caller with no project membership at all.

    Distinct from the no-token AuthError path: this context resolves without raising, so
    tool_visible's scope check itself must be what withholds accepted_fields, not merely
    the fail-closed AuthError branch.
    """
    return RequestContext(
        principal="outsider-user",
        agent_session="sess-outsider",
        projects=(),
        roles={},
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
# Test 3: bad arguments for inner tool → informative configuration_error (#2304)
# ---------------------------------------------------------------------------


def test_bad_arguments_is_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing required arguments for an inner tool yield an informative configuration_error.

    Parity target (ADR-0268): the gateway's schema-validation failure names the same
    field/kind detail a direct bind would raise, not a content-free envelope. The caller
    holds VIEWER on runs.get's required scope, so accepted_fields is disclosed too — see
    test_bad_arguments_without_visibility_omits_accepted_fields for the withheld case.
    """
    monkeypatch.setattr(gateway, "current_context", _viewer_ctx)
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
    assert "tools.search" in content["suggested_next_actions"]
    errors = content["data"]["field_errors"]
    assert {"field": "run_id", "kind": "missing_argument"} in errors
    # No caller-supplied value or pydantic ctx leaks through.
    for entry in errors:
        assert set(entry) == {"field", "kind"}
    accepted = content["data"]["accepted_fields"]
    assert "run_id" in accepted
    assert "include_console_artifacts" in accepted


# ---------------------------------------------------------------------------
# Test 3b: unexpected keyword argument → same informative shape (#2304)
# ---------------------------------------------------------------------------


def test_unexpected_argument_is_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown keyword argument yields the same field/kind detail, not just "missing".

    The caller holds VIEWER on runs.get's required scope, so field_errors is visible — see
    test_bad_arguments_without_visibility_omits_accepted_fields for the withheld case (#2325).
    """
    monkeypatch.setattr(gateway, "current_context", _viewer_ctx)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool(
            "tools.invoke",
            {"name": "runs.get", "arguments": {"run_id": "r-1", "duration_minutes": 5}},
        )

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["error_category"] == "configuration_error"
    errors = content["data"]["field_errors"]
    assert any(e["field"] == "duration_minutes" for e in errors)


# ---------------------------------------------------------------------------
# Test 3c: accepted_fields is withheld when the caller cannot see the tool (#2304)
# ---------------------------------------------------------------------------


def test_bad_arguments_without_visibility_omits_accepted_fields() -> None:
    """No verified caller context means no schema disclosure, matching tools.search's RBAC gate.

    Argument binding fails before an inner handler's own require_role check ever runs, so a
    caller who could not see runs.get's schema through tools.search must not get it here either.
    field_errors is withheld too (#2325): it names the same parameter as accepted_fields.
    """
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.invoke", {"name": "runs.get", "arguments": {}})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["error_category"] == "configuration_error"
    assert "accepted_fields" not in content["data"]
    assert "field_errors" not in content["data"]


def test_bad_arguments_scope_denied_omits_accepted_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """An authenticated caller with no project membership is also withheld the schema.

    Distinct from the no-token case above: tool_visible's own scope check must be what
    denies it, not only the fail-closed AuthError branch. field_errors is withheld here too
    (#2325): it would otherwise disclose a hidden tool's parameter name to this caller.
    """
    monkeypatch.setattr(gateway, "current_context", _no_grant_ctx)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.invoke", {"name": "runs.get", "arguments": {}})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["error_category"] == "configuration_error"
    assert "accepted_fields" not in content["data"]
    assert "field_errors" not in content["data"]


# ---------------------------------------------------------------------------
# Test 3d: field_errors stays bounded against an adversarial argument count (#2304)
# ---------------------------------------------------------------------------


def test_field_errors_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller sending many bad keyword arguments gets a capped, not unbounded, error list."""
    monkeypatch.setattr(gateway, "current_context", _viewer_ctx)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())
    bogus_args = {f"bogus_field_{i}": i for i in range(50)}

    async def _run() -> Any:
        return await app.call_tool(
            "tools.invoke", {"name": "runs.get", "arguments": {"run_id": "r-1", **bogus_args}}
        )

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["error_category"] == "configuration_error"
    assert len(content["data"]["field_errors"]) == gateway._FIELD_ERROR_LIMIT


# ---------------------------------------------------------------------------
# Test 3e: a body-raised bare ValidationError is not mislabeled as bad arguments (#2304)
# ---------------------------------------------------------------------------


def test_body_raised_validation_error_omits_field_detail() -> None:
    """A bare pydantic.ValidationError from a tool's own body is not an argument problem.

    fastmcp only wraps a *binding* failure in its own ValidationError; a bare
    pydantic.ValidationError escaping a tool body (e.g. re-validating data read from the
    database) reaches this branch unwrapped and must not be attributed to the caller's
    arguments — no fabricated field_errors/accepted_fields, and no tools.search pointer.
    """
    from pydantic import BaseModel

    app = FastMCP("test-gateway-body-validation-error")

    class _Row(BaseModel):
        kind: str

    @app.tool(name="data.corrupt")  # type: ignore[misc]
    async def _data_corrupt() -> ToolResponse:
        _Row.model_validate({"kind": 123, "unexpected": object()})
        raise AssertionError("model_validate should have raised")

    gateway.register(app, resolver=ProviderResolver({}))
    advertise_envelope_output_schema(app)

    async def _run() -> Any:
        return await app.call_tool("tools.invoke", {"name": "data.corrupt", "arguments": {}})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["error_category"] == "configuration_error"
    assert content["data"] == {}
    assert content["suggested_next_actions"] == []


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
