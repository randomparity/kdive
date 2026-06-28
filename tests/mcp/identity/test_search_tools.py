"""Behavior tests for the ``tools.search`` discovery tool (ADR-0267, #866)."""

from __future__ import annotations

from typing import cast

from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools.identity.search import ToolEntry, search_tools
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role

_BOOT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"run_id": {"type": "string"}},
}
_CATALOG = [
    ToolEntry("runs.boot", "Boot a built, installed Run and capture the console.", _BOOT_SCHEMA),
    ToolEntry("runs.build", "Build the kernel for a Run.", {"type": "object"}),
    ToolEntry("accounting.set_budget", "Set the project budget.", {"type": "object"}),
]


def _contributor() -> RequestContext:
    return RequestContext(
        principal="c", agent_session=None, projects=("p",), roles={"p": Role.CONTRIBUTOR}
    )


def _results(resp: ToolResponse) -> list[dict[str, JsonValue]]:
    return cast("list[dict[str, JsonValue]]", resp.data["results"])


def _names(resp: ToolResponse) -> list[JsonValue]:
    return [r["name"] for r in _results(resp)]


def test_search_returns_constructible_schema_for_match() -> None:
    """A capability phrase returns the matching tool with the exact registry input schema."""
    resp = search_tools(_CATALOG, _contributor(), "boot a kernel", limit=8)
    assert resp.status == "ok"
    results = _results(resp)
    assert "runs.boot" in _names(resp)
    boot = next(r for r in results if r["name"] == "runs.boot")
    assert boot["input_schema"] == _BOOT_SCHEMA
    assert boot["description"]
    assert resp.data["result_count"] == len(results)


def test_search_excludes_tools_the_caller_cannot_invoke() -> None:
    """An admin-only tool that matches textually is hidden from a contributor (RBAC filter)."""
    resp = search_tools(_CATALOG, _contributor(), "budget", limit=8)
    assert "accounting.set_budget" not in _names(resp)


def test_search_admin_sees_admin_tool() -> None:
    """The same query as an admin surfaces the admin-scoped tool."""
    admin = RequestContext(
        principal="a", agent_session=None, projects=("p",), roles={"p": Role.ADMIN}
    )
    resp = search_tools(_CATALOG, admin, "budget", limit=8)
    assert "accounting.set_budget" in _names(resp)


def test_search_empty_query_is_configuration_error() -> None:
    """An empty/whitespace query is rejected, pointing at the namespace TOC."""
    resp = search_tools(_CATALOG, _contributor(), "   ", limit=8)
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    assert resp.data["reason"] == "empty_query"


def test_search_zero_results_is_success_with_empty_list() -> None:
    """A well-formed query that matches nothing returns success with no results."""
    resp = search_tools(_CATALOG, _contributor(), "zzzqqq nonsense", limit=8)
    assert resp.status == "ok"
    assert _results(resp) == []
    assert resp.data["result_count"] == 0


def test_search_respects_limit() -> None:
    """No more than ``limit`` results are returned."""
    resp = search_tools(_CATALOG, _contributor(), "run", limit=1)
    assert len(_results(resp)) <= 1
