"""Cover the tool-exposure filtering middleware."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kdive.mcp.exposure import CORE_TOOLS
from kdive.mcp.middleware import exposure as exposure_mod
from kdive.mcp.middleware.exposure import ToolExposureMiddleware
from kdive.security.authz.errors import AuthError


def _tools(*names: str) -> list[Any]:
    return [SimpleNamespace(name=name) for name in names]


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
    authz_ctx = object()
    visible_ctxs: list[Any] = []
    monkeypatch.setattr(exposure_mod, "request_context", lambda: authz_ctx)

    def _visible(ctx: Any, names: Any) -> set[str]:
        visible_ctxs.append(ctx)
        assert list(names) == ["runs.create", "runs.get", "admin.teardown"]
        return {"runs.create", "runs.get"}

    monkeypatch.setattr(exposure_mod, "visible_tool_names", _visible)

    result, list_context, received = _run(ToolExposureMiddleware(), tools)

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

    result, _ctx, _received = _run(ToolExposureMiddleware(), tools)

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

    result, _ctx, _received = _run(ToolExposureMiddleware(), tools)

    assert [t.name for t in result] == ["runs.create", "admin.teardown"]
    (args, kwargs) = warnings[0]
    assert args == ("tool-exposure filter failed; advertising the full catalog",)
    assert kwargs["exc_info"] is True


# ---------------------------------------------------------------------------
# KDIVE_MCP_TOOL_GATEWAY flag tests
# ---------------------------------------------------------------------------


def test_gateway_off_returns_full_rbac_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gateway flag is absent the full RBAC-scoped catalog is returned unchanged."""
    # 25 synthetic tools — deliberately more than the 10-member CORE_TOOLS set
    many_tools = _tools(*(f"tool_{i}" for i in range(25)))
    monkeypatch.delenv("KDIVE_MCP_TOOL_GATEWAY", raising=False)
    monkeypatch.setattr(exposure_mod, "request_context", lambda: object())
    monkeypatch.setattr(exposure_mod, "visible_tool_names", lambda _ctx, names: set(names))

    result, _, _ = _run(ToolExposureMiddleware(), many_tools)

    assert len(result) > 20


def test_gateway_on_returns_core_intersect_rbac(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gateway is on, list_tools returns only RBAC-visible ∩ CORE_TOOLS."""
    # RBAC passes everything; the gateway should clip to CORE_TOOLS
    core_plus_extras = list(CORE_TOOLS) + ["admin.delete", "inventory.list", "ops.diagnostics"]
    tools = _tools(*core_plus_extras)
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "on")
    monkeypatch.setattr(exposure_mod, "request_context", lambda: object())
    monkeypatch.setattr(exposure_mod, "visible_tool_names", lambda _ctx, names: set(names))

    result, _, _ = _run(ToolExposureMiddleware(), tools)
    names = {t.name for t in result}

    assert names <= CORE_TOOLS
    assert {"tools.search", "tools.invoke", "runs.build_install_boot"} <= names


def test_gateway_on_fails_open_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gateway is on but the try-block raises, the full catalog is returned."""
    all_tools = _tools("runs.create", "admin.teardown")
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "on")
    monkeypatch.setattr(exposure_mod, "request_context", lambda: object())

    def _boom(_ctx: Any, _names: Any) -> set[str]:
        raise RuntimeError("rbac exploded with gateway on")

    monkeypatch.setattr(exposure_mod, "visible_tool_names", _boom)

    result, _, _ = _run(ToolExposureMiddleware(), all_tools)

    assert [t.name for t in result] == ["runs.create", "admin.teardown"]
