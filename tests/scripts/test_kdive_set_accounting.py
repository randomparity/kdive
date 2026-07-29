"""Behavioral tests for scripts/kdive_set_accounting.py (no live server)."""

from __future__ import annotations

import asyncio
from typing import Any

import scripts.kdive_set_accounting as acct


class _FakeResult:
    def __init__(self, payload: dict[str, Any] | None, *, is_error: bool = False) -> None:
        self.is_error = is_error
        self.structured_content = payload


class _FakeClient:
    """Records call_tool invocations; satisfies the async-context-manager protocol."""

    calls: list[tuple[str, dict[str, Any]]] = []
    fail_names: set[str] = set()
    # Names whose result is a *returned* failure envelope (is_error stays False, ADR-0089) —
    # the shape an authorization denial takes since ADR-0486.
    denied_names: set[str] = set()

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any], *, raise_on_error: bool):
        type(self).calls.append((name, arguments))
        if name in type(self).denied_names:
            return _FakeResult({"object": "error", "error_category": "authorization_denied"})
        if name in type(self).fail_names:
            # A raised tool error: fastmcp sets the transport flag and carries no envelope.
            return _FakeResult(None, is_error=True)
        return _FakeResult({"object": "ok", "data": dict(arguments)})


def test_build_calls_uses_flat_quota_params_and_defaults() -> None:
    ns = acct.parse(["--base", "http://h/mcp"])
    calls = acct.build_calls(ns)
    names = [n for n, _ in calls]
    assert names == ["accounting.set_quota", "accounting.set_budget", "accounting.usage"]
    quota = dict(calls)["accounting.set_quota"]
    assert quota == {
        "project": "demo",
        "max_concurrent_allocations": 4,
        "max_concurrent_systems": 4,
        "max_pending_allocations": 0,
    }
    assert dict(calls)["accounting.set_budget"] == {"project": "demo", "limit_kcu": "1000000"}


def test_parse_accepts_full_long_form_argv_from_setup_scripts() -> None:
    # setup-{local,remote}-libvirt.sh emit exactly these long-form flags. A helper-side
    # rename/removal must break here rather than at an operator's first run, where it would
    # surface as "unrecognized arguments" (exit 2) and re-create the quota_exceeded dead-end.
    ns = acct.parse(
        [
            "--base",
            "http://h/mcp",
            "--project",
            "demo",
            "--limit-kcu",
            "1000000",
            "--max-concurrent-allocations",
            "4",
            "--max-concurrent-systems",
            "4",
        ]
    )
    assert ns.base == "http://h/mcp"
    assert ns.project == "demo"
    assert ns.limit_kcu == "1000000"
    assert ns.max_alloc == 4
    assert ns.max_sys == 4
    assert [n for n, _ in acct.build_calls(ns)] == [
        "accounting.set_quota",
        "accounting.set_budget",
        "accounting.usage",
    ]


def test_run_invokes_three_tools_with_bearer(monkeypatch) -> None:
    _FakeClient.calls = []
    monkeypatch.setattr(acct, "Client", _FakeClient)
    ns = acct.parse(["--base", "http://h/mcp", "--token", "T", "--project", "acme"])
    rc = asyncio.run(acct.run(ns))
    assert rc == 0
    assert [n for n, _ in _FakeClient.calls] == [
        "accounting.set_quota",
        "accounting.set_budget",
        "accounting.usage",
    ]


def test_run_stops_and_returns_1_on_first_tool_error(monkeypatch) -> None:
    _FakeClient.calls = []
    monkeypatch.setattr(_FakeClient, "fail_names", {"accounting.set_quota"})
    monkeypatch.setattr(acct, "Client", _FakeClient)
    ns = acct.parse(["--base", "http://h/mcp", "--token", "T", "--project", "acme"])
    rc = asyncio.run(acct.run(ns))
    assert rc == 1
    # The loop stops at the first failure; set_budget / usage are never attempted.
    assert [n for n, _ in _FakeClient.calls] == ["accounting.set_quota"]


def test_run_treats_a_returned_denial_envelope_as_a_failure(monkeypatch, capsys) -> None:
    # Since ADR-0486 an authorization denial arrives as a returned envelope with is_error False.
    # Branching on the transport flag alone would run every call and exit 0 having written
    # nothing; the helper reads error_category, stops, and dumps the envelope as the diagnostic.
    _FakeClient.calls = []
    monkeypatch.setattr(_FakeClient, "denied_names", {"accounting.set_quota"})
    monkeypatch.setattr(acct, "Client", _FakeClient)
    ns = acct.parse(["--base", "http://h/mcp", "--token", "T", "--project", "acme"])
    rc = asyncio.run(acct.run(ns))
    assert rc == 1
    assert [n for n, _ in _FakeClient.calls] == ["accounting.set_quota"]
    err = capsys.readouterr().err
    assert "error: tool accounting.set_quota failed" in err
    assert "authorization_denied" in err, "the envelope is the whole diagnostic; do not drop it"


def test_run_prints_no_bare_null_when_the_failure_carries_no_envelope(monkeypatch, capsys) -> None:
    # The is_error path carries no structured content. Dumping it unconditionally emits a bare
    # `null` line the pre-ADR-0486 helper never printed — noise, not a diagnostic. Fails if the
    # suppression is removed.
    _FakeClient.calls = []
    monkeypatch.setattr(_FakeClient, "fail_names", {"accounting.set_quota"})
    monkeypatch.setattr(acct, "Client", _FakeClient)
    ns = acct.parse(["--base", "http://h/mcp", "--token", "T", "--project", "acme"])
    assert asyncio.run(acct.run(ns)) == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines == ["error: tool accounting.set_quota failed"]


def test_run_without_token_exits_2(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_TOKEN", raising=False)
    ns = acct.parse(["--base", "http://h/mcp"])
    assert asyncio.run(acct.run(ns)) == 2
