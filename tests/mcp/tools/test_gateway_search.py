"""TDD tests for the ``tools.search`` gateway discovery tool (ADR-0268, #866).

Coverage:
* A keyword query returns the relevant tool.
* Namespace browse returns all tools in that plane.
* The result set is hard-capped and ``truncated`` is True when results were cut.
* RBAC filters: admin-only tools do not appear for a viewer-role caller.
* Each match carries the full ``input_schema`` so ``tools.invoke`` can be called.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _verifier() -> JWTVerifier:
    kp = make_keypair()
    return JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)


def _operator_ctx() -> RequestContext:
    return RequestContext(
        principal="operator-user",
        agent_session="sess-op",
        projects=("proj-a",),
        roles={"proj-a": Role.OPERATOR},
    )


def _viewer_ctx() -> RequestContext:
    return RequestContext(
        principal="viewer-user",
        agent_session="sess-viewer",
        projects=("proj-a",),
        roles={"proj-a": Role.VIEWER},
    )


def _secret_registry() -> Any:
    from kdive.security.secrets.secret_registry import SecretRegistry

    return SecretRegistry()


def _call_result(result: Any) -> dict[str, Any]:
    """Extract the structured_content dict from an app.call_tool result."""
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict), f"expected structured_content dict, got {result!r}"
    return structured


# ---------------------------------------------------------------------------
# Test 1: keyword query surfaces the relevant tool
# ---------------------------------------------------------------------------


def test_query_ranks_relevant_tool_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """A query for 'boot a built kernel' returns runs.boot in the match list."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": "boot a built kernel"})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = [m["name"] for m in content["data"]["matches"]]
    assert "runs.boot" in names, f"runs.boot not in {names}"


# ---------------------------------------------------------------------------
# Test 2: namespace browse returns all tools in the plane
# ---------------------------------------------------------------------------


def test_namespace_browse_returns_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Namespace='debug' returns all debug.* tools visible to an operator."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"namespace": "debug", "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = {m["name"] for m in content["data"]["matches"]}
    assert {"debug.read_memory", "debug.set_breakpoint"} <= names, (
        f"expected debug.read_memory and debug.set_breakpoint in {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# Test 3: result payload is hard-capped; truncated reflects overflow
# ---------------------------------------------------------------------------


def test_payload_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """limit=3 on a namespace with >3 tools yields 3 matches and truncated=True."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"namespace": "debug", "limit": 3})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert len(content["data"]["matches"]) == 3
    assert content["data"]["truncated"] is True


# ---------------------------------------------------------------------------
# Test 4: RBAC filter hides admin-only tools from a viewer
# ---------------------------------------------------------------------------


def test_results_rbac_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """control.force_crash (admin-only) does not appear in a viewer's search results."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _viewer_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": "force crash"})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = {m["name"] for m in content["data"]["matches"]}
    assert "control.force_crash" not in names, (
        "control.force_crash must not be visible to a viewer-role caller"
    )


# ---------------------------------------------------------------------------
# Test 5: every match includes the full input schema
# ---------------------------------------------------------------------------


def test_match_includes_full_input_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """A match for runs.get carries its full input_schema (including 'run_id')."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": "get a run"})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    matches = content["data"]["matches"]
    runs_get = next((m for m in matches if m["name"] == "runs.get"), None)
    assert runs_get is not None, f"runs.get not found in matches: {[m['name'] for m in matches]}"
    assert "run_id" in str(runs_get["input_schema"]), (
        f"run_id not in input_schema: {runs_get['input_schema']}"
    )
