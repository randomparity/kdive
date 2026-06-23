"""Cover the usage-tracking middleware: outcome classification + usage-row construction."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware import usage as usage_mod
from kdive.mcp.middleware.shared import ToolOutcome
from kdive.mcp.middleware.usage import UsageTrackingMiddleware, _call_project
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.rbac import AuthorizationError


def _context(name: str = "runs.create", arguments: Any = None) -> Any:
    return SimpleNamespace(message=SimpleNamespace(name=name, arguments=arguments))


def test_call_project_returns_non_empty_string() -> None:
    assert _call_project(_context(arguments={"project": "demo"})) == "demo"


def test_call_project_none_for_empty_string() -> None:
    assert _call_project(_context(arguments={"project": ""})) is None


def test_call_project_none_for_non_string() -> None:
    assert _call_project(_context(arguments={"project": 5})) is None


def test_call_project_none_when_no_arguments_dict() -> None:
    assert _call_project(_context(arguments=None)) is None


def test_classify_ok_when_no_error_category() -> None:
    result = ToolResponse.success("runs.create", "created")
    assert UsageTrackingMiddleware._classify(result) is ToolOutcome.OK


def test_classify_denied_for_authorization_denied() -> None:
    result = ToolResponse.failure("runs.create", ErrorCategory.AUTHORIZATION_DENIED)
    assert UsageTrackingMiddleware._classify(result) is ToolOutcome.DENIED


def test_classify_error_for_other_category() -> None:
    result = ToolResponse.failure("runs.create", ErrorCategory.CONFIGURATION_ERROR)
    assert UsageTrackingMiddleware._classify(result) is ToolOutcome.ERROR


def _spy_middleware() -> tuple[UsageTrackingMiddleware, list[tuple[Any, ToolOutcome]]]:
    mw = UsageTrackingMiddleware(pool=object())
    recorded: list[tuple[Any, ToolOutcome]] = []

    async def _record(ctx: Any, outcome: ToolOutcome) -> None:
        recorded.append((ctx, outcome))

    mw._record = _record  # type: ignore[method-assign]
    return mw, recorded


def test_on_call_tool_records_classified_outcome_on_success() -> None:
    mw, recorded = _spy_middleware()
    ctx = _context()
    seen: list[Any] = []

    async def call_next(passed: Any) -> ToolResponse:
        seen.append(passed)
        return ToolResponse.failure("runs.create", ErrorCategory.CONFIGURATION_ERROR)

    asyncio.run(mw.on_call_tool(ctx, call_next))
    assert seen == [ctx]  # call_next received the real context, not None
    assert recorded == [(ctx, ToolOutcome.ERROR)]


def test_on_call_tool_records_denied_and_reraises_on_authorization_error() -> None:
    mw, recorded = _spy_middleware()
    ctx = _context()

    async def call_next(_ctx: Any) -> ToolResponse:
        raise AuthorizationError("nope")

    with pytest.raises(AuthorizationError):
        asyncio.run(mw.on_call_tool(ctx, call_next))
    assert recorded == [(ctx, ToolOutcome.DENIED)]


def test_on_call_tool_records_error_and_reraises_on_other_exception() -> None:
    mw, recorded = _spy_middleware()
    ctx = _context()

    async def call_next(_ctx: Any) -> ToolResponse:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(mw.on_call_tool(ctx, call_next))
    assert recorded == [(ctx, ToolOutcome.ERROR)]


class _FakeConn:
    def transaction(self) -> Any:
        @asynccontextmanager
        async def _txn() -> Any:
            yield None

        return _txn()


class _FakePool:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def connection(self, *, timeout: float) -> Any:
        self.timeout = timeout

        @asynccontextmanager
        async def _conn() -> Any:
            yield _FakeConn()

        return _conn()


def _patch_record_boundary(
    monkeypatch: pytest.MonkeyPatch, events: list[Any], actor_ctxs: list[Any]
) -> Any:
    ctx = SimpleNamespace(
        principal="alice",
        agent_session="sess-1",
        client_id="client-1",
    )
    monkeypatch.setattr(usage_mod, "request_context", lambda: ctx)

    def _actor_for(passed: Any) -> str:
        actor_ctxs.append(passed)
        return "actor-1"

    monkeypatch.setattr(usage_mod, "actor_for", _actor_for)

    async def _record_usage(conn: Any, event: Any) -> None:
        events.append((conn, event))

    monkeypatch.setattr(usage_mod, "record_usage", _record_usage)
    return ctx


def test_record_builds_usage_event_from_context(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    actor_ctxs: list[Any] = []
    ctx = _patch_record_boundary(monkeypatch, events, actor_ctxs)
    pool = _FakePool()
    mw = UsageTrackingMiddleware(pool=pool, acquire_timeout=2.0)

    asyncio.run(mw._record(_context(arguments={"project": "demo"}), ToolOutcome.OK))

    assert pool.timeout == 2.0
    assert actor_ctxs == [ctx]  # actor_for received the verified context
    ((conn, event),) = events
    assert isinstance(conn, _FakeConn)  # record_usage ran on the opened connection
    assert event.principal == "alice"
    assert event.agent_session == "sess-1"
    assert event.client_id == "client-1"
    assert event.project == "demo"
    assert event.tool == "runs.create"
    assert event.outcome == "ok"
    assert event.actor == "actor-1"


def test_record_uses_question_mark_tool_when_message_has_no_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    _patch_record_boundary(monkeypatch, events, [])
    mw = UsageTrackingMiddleware(pool=_FakePool())
    context = SimpleNamespace(message=SimpleNamespace())  # message lacks `name`

    asyncio.run(mw._record(context, ToolOutcome.OK))

    ((_conn, event),) = events
    assert event.tool == "?"


def test_record_default_acquire_timeout_is_one_second(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    _patch_record_boundary(monkeypatch, events, [])
    pool = _FakePool()
    mw = UsageTrackingMiddleware(pool=pool)  # no acquire_timeout override

    asyncio.run(mw._record(_context(), ToolOutcome.OK))

    assert pool.timeout == 1.0


def test_record_swallows_failures_best_effort_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        usage_mod, "request_context", lambda: (_ for _ in ()).throw(RuntimeError("no ctx"))
    )
    warnings: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        usage_mod._log,
        "warning",
        lambda *a, **k: warnings.append((a, k)),
    )
    mw = UsageTrackingMiddleware(pool=_FakePool())

    # a failure inside _record must never propagate (best-effort recording)
    asyncio.run(mw._record(_context(name="runs.create"), ToolOutcome.OK))

    (args, kwargs) = warnings[0]
    assert args[0] == "usage recording failed for tool %s"
    assert args[1] == "runs.create"  # the tool name is logged
    assert kwargs["exc_info"] is True
