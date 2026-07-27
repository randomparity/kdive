"""Cover the tool-exposure filtering middleware."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from kdive.mcp.exposure import CORE_TOOLS
from kdive.mcp.middleware import exposure as exposure_mod
from kdive.mcp.middleware.exposure import ToolExposureMiddleware
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.errors import AuthError
from kdive.security.authz.rbac import PlatformRole, Role


def _tools(*names: str) -> list[Any]:
    return [SimpleNamespace(name=name) for name in names]


def _ctx(
    *,
    agent_session: str | None = "agent-1",
    client_id: str | None = None,
    roles: dict[str, Role] | None = None,
    platform: frozenset[PlatformRole] = frozenset(),
) -> RequestContext:
    roles = roles or {}
    return RequestContext(
        principal="p",
        agent_session=agent_session,
        projects=tuple(roles),
        roles=roles,
        platform_roles=platform,
        client_id=client_id,
    )


def _run(mw: ToolExposureMiddleware, tools: list[Any]) -> tuple[list[Any], Any, list[Any]]:
    """Drive on_list_tools; return (result, list-context, contexts call_next received)."""
    list_context = object()
    received: list[Any] = []

    async def call_next(passed: Any) -> list[Any]:
        received.append(passed)
        return tools

    result = list(asyncio.run(mw.on_list_tools(list_context, call_next)))
    return result, list_context, received


def test_filters_to_visible_tool_names_threading_both_contexts(monkeypatch) -> None:
    tools = _tools("runs.create", "runs.get", "admin.teardown")
    authz_ctx = _ctx()
    visible_ctxs: list[Any] = []
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "off")
    monkeypatch.setattr(exposure_mod, "request_context", lambda: authz_ctx)

    def _visible(ctx: Any, names: Any) -> set[str]:
        visible_ctxs.append(ctx)
        assert list(names) == ["runs.create", "runs.get", "admin.teardown"]
        return {"runs.create", "runs.get"}

    monkeypatch.setattr(exposure_mod, "visible_tool_names", _visible)

    result, list_context, received = _run(ToolExposureMiddleware(ProviderResolver({})), tools)

    assert [t.name for t in result] == ["runs.create", "runs.get"]
    assert received == [list_context]  # call_next got the list context, not None
    assert visible_ctxs == [authz_ctx]  # the verified context, not None


def test_auth_error_advertises_full_catalog_and_debug_logs(monkeypatch) -> None:
    tools = _tools("runs.create", "admin.teardown")

    def _raise() -> Any:
        raise AuthError("no token")

    monkeypatch.setattr(exposure_mod, "request_context", _raise)
    debugs: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(exposure_mod._log, "debug", lambda *a, **k: debugs.append((a, k)))

    result, _ctx, _received = _run(ToolExposureMiddleware(ProviderResolver({})), tools)

    assert [t.name for t in result] == ["runs.create", "admin.teardown"]
    assert debugs[0][0] == ("no verified token in on_list_tools; advertising the full catalog",)


def test_unexpected_error_advertises_full_catalog_and_warns(monkeypatch) -> None:
    tools = _tools("runs.create", "admin.teardown")
    monkeypatch.setattr(exposure_mod, "request_context", lambda: object())

    def _boom(_ctx: Any, _names: Any) -> set[str]:
        raise RuntimeError("filter exploded")

    monkeypatch.setattr(exposure_mod, "visible_tool_names", _boom)
    warnings: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(exposure_mod._log, "warning", lambda *a, **k: warnings.append((a, k)))

    result, _ctx, _received = _run(ToolExposureMiddleware(ProviderResolver({})), tools)

    assert [t.name for t in result] == ["runs.create", "admin.teardown"]
    (args, kwargs) = warnings[0]
    assert args == ("tool-exposure filter failed; advertising the full catalog",)
    assert kwargs["exc_info"] is True


# ---------------------------------------------------------------------------
# KDIVE_MCP_TOOL_GATEWAY flag tests
# ---------------------------------------------------------------------------


def test_gateway_off_returns_full_rbac_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gateway flag is off the full RBAC-scoped catalog is returned unchanged."""
    # 25 synthetic tools — deliberately more than the 9-member CORE_TOOLS set
    many_tools = _tools(*(f"tool_{i}" for i in range(25)))
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "off")
    monkeypatch.setattr(exposure_mod, "request_context", _ctx)
    monkeypatch.setattr(exposure_mod, "visible_tool_names", lambda _ctx, names: set(names))

    result, _, _ = _run(ToolExposureMiddleware(ProviderResolver({})), many_tools)

    assert len(result) > 20


def test_gateway_on_returns_core_intersect_rbac(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gateway is on, list_tools returns only RBAC-visible ∩ CORE_TOOLS."""
    # RBAC passes everything; the gateway should clip to CORE_TOOLS
    core_plus_extras = list(CORE_TOOLS) + ["admin.delete", "inventory.list", "ops.diagnostics"]
    tools = _tools(*core_plus_extras)
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "on")
    monkeypatch.setattr(exposure_mod, "request_context", _ctx)
    monkeypatch.setattr(exposure_mod, "visible_tool_names", lambda _ctx, names: set(names))

    result, _, _ = _run(ToolExposureMiddleware(ProviderResolver({})), tools)
    names = {t.name for t in result}

    assert names <= CORE_TOOLS
    assert {"tools.search", "tools.invoke", "runs.create"} <= names


def test_gateway_on_fails_open_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gateway is on but the try-block raises, the full catalog is returned."""
    all_tools = _tools("runs.create", "admin.teardown")
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "on")
    monkeypatch.setattr(exposure_mod, "request_context", lambda: object())

    def _boom(_ctx: Any, _names: Any) -> set[str]:
        raise RuntimeError("rbac exploded with gateway on")

    monkeypatch.setattr(exposure_mod, "visible_tool_names", _boom)

    result, _, _ = _run(ToolExposureMiddleware(ProviderResolver({})), all_tools)

    assert [t.name for t in result] == ["runs.create", "admin.teardown"]


def test_operator_cli_gets_direct_rbac_catalog_when_gateway_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tools("tools.search", "ops.diagnostics", "jobs.get")
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "on")
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "kdivectl")
    monkeypatch.setattr(
        exposure_mod,
        "request_context",
        lambda: _ctx(
            agent_session=None,
            client_id="kdivectl",
            platform=frozenset({PlatformRole.PLATFORM_OPERATOR}),
        ),
    )

    result, _, _ = _run(ToolExposureMiddleware(ProviderResolver({})), tools)
    names = {t.name for t in result}

    assert names == {"tools.search", "ops.diagnostics"}
    assert "ops.diagnostics" not in CORE_TOOLS


def test_unknown_client_with_platform_role_stays_on_gateway_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tools("tools.search", "ops.diagnostics")
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "on")
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "kdivectl")
    monkeypatch.setattr(
        exposure_mod,
        "request_context",
        lambda: _ctx(
            agent_session=None,
            client_id="mystery",
            platform=frozenset({PlatformRole.PLATFORM_OPERATOR}),
        ),
    )

    result, _, _ = _run(ToolExposureMiddleware(ProviderResolver({})), tools)

    assert {t.name for t in result} == {"tools.search"}


def test_profile_resolution_failure_falls_back_to_agent_gateway(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tools = _tools("tools.search", "ops.diagnostics")
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "on")
    monkeypatch.setattr(
        exposure_mod,
        "request_context",
        lambda: _ctx(platform=frozenset({PlatformRole.PLATFORM_OPERATOR})),
    )
    monkeypatch.setattr(exposure_mod, "visible_tool_names", lambda _ctx, names: set(names))

    def _raise_profile(_ctx: RequestContext) -> object:
        raise RuntimeError("profile resolver exploded")

    monkeypatch.setattr(exposure_mod, "resolve_exposure_profile", _raise_profile)
    profile_counter_calls: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(
        exposure_mod._PROFILE_SELECTIONS,
        "add",
        lambda amount, attrs: profile_counter_calls.append((amount, attrs)),
    )
    exposure_counter_calls: list[int] = []
    monkeypatch.setattr(
        exposure_mod._EXPOSURE_FAILOPEN, "add", lambda amount: exposure_counter_calls.append(amount)
    )

    with caplog.at_level(logging.WARNING, logger="kdive.mcp.middleware.exposure"):
        result, _, _ = _run(ToolExposureMiddleware(ProviderResolver({})), tools)

    assert {t.name for t in result} == {"tools.search"}
    assert profile_counter_calls == [(1, {"profile": "agent_gateway", "source": "fallback"})]
    assert exposure_counter_calls == []
    assert "profile resolution failed" in caplog.text
