"""Behavioral tests for scripts/kdive_set_accounting.py (no live server)."""

from __future__ import annotations

import asyncio
from typing import Any

import scripts.kdive_set_accounting as acct


class _FakeResult:
    def __init__(self, payload: dict[str, Any], *, is_error: bool = False) -> None:
        self.is_error = is_error
        self.structured_content = payload


class _FakeClient:
    """Records call_tool invocations; satisfies the async-context-manager protocol."""

    calls: list[tuple[str, dict[str, Any]]] = []
    fail_names: set[str] = set()

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any], *, raise_on_error: bool):
        type(self).calls.append((name, arguments))
        return _FakeResult(
            {"object": "ok", "data": dict(arguments)}, is_error=name in type(self).fail_names
        )


def test_build_calls_uses_flat_quota_params_and_defaults() -> None:
    ns = acct.parse(["--base", "http://h/mcp"])
    calls = acct.build_calls(ns)
    names = [n for n, _ in calls]
    assert names == ["accounting.set_quota", "accounting.set_budget", "accounting.usage_project"]
    quota = dict(calls)["accounting.set_quota"]
    assert quota == {
        "project": "demo",
        "max_concurrent_allocations": 4,
        "max_concurrent_systems": 4,
        "max_pending_allocations": 0,
    }
    assert dict(calls)["accounting.set_budget"] == {"project": "demo", "limit_kcu": "1000000"}


def test_run_invokes_three_tools_with_bearer(monkeypatch) -> None:
    _FakeClient.calls = []
    monkeypatch.setattr(acct, "Client", _FakeClient)
    ns = acct.parse(["--base", "http://h/mcp", "--token", "T", "--project", "acme"])
    rc = asyncio.run(acct.run(ns))
    assert rc == 0
    assert [n for n, _ in _FakeClient.calls] == [
        "accounting.set_quota",
        "accounting.set_budget",
        "accounting.usage_project",
    ]


def test_run_stops_and_returns_1_on_first_tool_error(monkeypatch) -> None:
    _FakeClient.calls = []
    monkeypatch.setattr(_FakeClient, "fail_names", {"accounting.set_quota"})
    monkeypatch.setattr(acct, "Client", _FakeClient)
    ns = acct.parse(["--base", "http://h/mcp", "--token", "T", "--project", "acme"])
    rc = asyncio.run(acct.run(ns))
    assert rc == 1
    # The loop stops at the first failure; set_budget / usage_project are never attempted.
    assert [n for n, _ in _FakeClient.calls] == ["accounting.set_quota"]


def test_run_without_token_exits_2(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_TOKEN", raising=False)
    ns = acct.parse(["--base", "http://h/mcp"])
    assert asyncio.run(acct.run(ns)) == 2
