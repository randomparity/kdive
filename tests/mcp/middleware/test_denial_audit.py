"""Cover the authorization-denial audit middleware."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware import denial_audit as da
from kdive.mcp.middleware.denial_audit import (
    _DROP_ARGUMENT,
    DenialAuditMiddleware,
    _audit_args_from_message,
    _current_agent_session,
    _json_argument,
)
from kdive.mcp.middleware.shared import result_error_category
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.errors import ProjectMembershipDenied
from kdive.security.authz.rbac import Role, RoleDenied


def _denial() -> RoleDenied:
    return RoleDenied(principal="alice", project="demo", held=Role.VIEWER, required=Role.OPERATOR)


# --- _current_agent_session -------------------------------------------------


def test_current_agent_session_reads_verified_token(monkeypatch) -> None:
    monkeypatch.setattr(da, "request_context", lambda: SimpleNamespace(agent_session="sess-9"))
    assert _current_agent_session() == "sess-9"


# --- _json_argument ---------------------------------------------------------


def test_json_argument_passes_scalars_through() -> None:
    assert _json_argument(None) is None
    assert _json_argument("x") == "x"
    assert _json_argument(True) is True
    assert _json_argument(7) == 7


def test_json_argument_keeps_finite_float_drops_non_finite() -> None:
    assert _json_argument(1.5) == 1.5
    assert _json_argument(float("inf")) is _DROP_ARGUMENT
    assert _json_argument(float("nan")) is _DROP_ARGUMENT


def test_json_argument_recurses_into_clean_list() -> None:
    assert _json_argument(["a", 1, None]) == ["a", 1, None]


def test_json_argument_drops_whole_list_when_an_element_is_unsafe() -> None:
    assert _json_argument(["a", float("inf")]) is _DROP_ARGUMENT


def test_json_argument_recurses_into_clean_dict() -> None:
    assert _json_argument({"k": "v", "n": 2}) == {"k": "v", "n": 2}


def test_json_argument_drops_dict_with_non_string_key() -> None:
    assert _json_argument({1: "v"}) is _DROP_ARGUMENT


def test_json_argument_drops_dict_when_a_value_is_unsafe() -> None:
    assert _json_argument({"k": float("nan")}) is _DROP_ARGUMENT


def test_json_argument_drops_unsupported_type() -> None:
    assert _json_argument(object()) is _DROP_ARGUMENT


# --- _audit_args_from_message -----------------------------------------------


def test_audit_args_empty_when_no_arguments() -> None:
    assert _audit_args_from_message(SimpleNamespace()) == {}


def test_audit_args_empty_when_arguments_not_a_dict() -> None:
    assert _audit_args_from_message(SimpleNamespace(arguments="oops")) == {}


def test_audit_args_keeps_safe_skips_non_str_key_and_unsafe_value() -> None:
    # the safe key *after* the non-str key must still be kept (skip-and-continue, not break)
    message = SimpleNamespace(
        arguments={"keep": "v", 9: "skip-key", "after": "w", "bad": float("inf")}
    )
    assert _audit_args_from_message(message) == {"keep": "v", "after": "w"}


# --- on_call_tool -----------------------------------------------------------


def _context(name: str = "admin.teardown", arguments: Any = None) -> Any:
    return SimpleNamespace(message=SimpleNamespace(name=name, arguments=arguments))


def _pool(value: object) -> AsyncConnectionPool:
    return cast("AsyncConnectionPool", value)


def _spy_middleware() -> tuple[DenialAuditMiddleware, list[Any]]:
    mw = DenialAuditMiddleware(pool=_pool(object()))
    recorded: list[Any] = []

    async def _record(tool: str, denial: RoleDenied, *, args: Any = None) -> None:
        recorded.append((tool, denial, args))

    mw._record = _record  # ty: ignore[invalid-assignment]
    return mw, recorded


def test_on_call_tool_passes_through_when_no_denial() -> None:
    mw, recorded = _spy_middleware()
    ok = ToolResponse.success("admin.teardown", "done")
    ctx = _context()
    seen: list[Any] = []

    async def call_next(passed: Any) -> ToolResponse:
        seen.append(passed)
        return ok

    assert asyncio.run(mw.on_call_tool(ctx, call_next)) is ok
    assert seen == [ctx]  # call_next received the real context, not None
    assert recorded == []


def test_on_call_tool_audits_role_denied_and_envelopes() -> None:
    mw, recorded = _spy_middleware()
    denial = _denial()

    async def call_next(_ctx: Any) -> ToolResponse:
        raise denial

    result = asyncio.run(mw.on_call_tool(_context(arguments={"force": True}), call_next))

    assert result_error_category(result) == ErrorCategory.AUTHORIZATION_DENIED.value
    (tool, recorded_denial, args) = recorded[0]
    assert tool == "admin.teardown"
    assert recorded_denial is denial
    assert args == {"force": True}  # sanitized call args reach the audit record


def test_on_call_tool_envelopes_even_when_audit_record_fails(monkeypatch) -> None:
    mw = DenialAuditMiddleware(pool=_pool(object()))

    async def _failing_record(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db down")

    mw._record = _failing_record  # ty: ignore[invalid-assignment]
    warnings: list[Any] = []
    monkeypatch.setattr(da._log, "warning", lambda *a, **k: warnings.append((a, k)))

    async def call_next(_ctx: Any) -> ToolResponse:
        raise _denial()

    result = asyncio.run(mw.on_call_tool(_context(), call_next))

    assert result_error_category(result) == ErrorCategory.AUTHORIZATION_DENIED.value
    (args, kwargs) = warnings[0]  # the audit failure was logged, not raised
    assert args[0] == "failed to audit RoleDenied for tool %s"
    assert args[1] == "admin.teardown"
    assert kwargs["exc_info"] is True


def test_on_call_tool_envelopes_project_membership_denied() -> None:
    mw, recorded = _spy_middleware()

    async def call_next(_ctx: Any) -> ToolResponse:
        raise ProjectMembershipDenied("not a member")

    result = asyncio.run(mw.on_call_tool(_context(name="runs.create"), call_next))

    assert result_error_category(result) == ErrorCategory.AUTHORIZATION_DENIED.value
    assert recorded == []  # membership denial is enveloped, not RoleDenied-audited


# --- _record ----------------------------------------------------------------


class _FakeConn:
    def transaction(self) -> Any:
        @asynccontextmanager
        async def _txn() -> Any:
            yield None

        return _txn()


class _FakePool:
    def connection(self) -> Any:
        @asynccontextmanager
        async def _conn() -> Any:
            yield _FakeConn()

        return _conn()


def test_record_builds_denial_event(monkeypatch) -> None:
    events: list[Any] = []

    async def _record_denial(conn: Any, *, event: Any) -> None:
        events.append((conn, event))

    monkeypatch.setattr(da.audit, "record_denial", _record_denial)
    mw = DenialAuditMiddleware(pool=_pool(_FakePool()), agent_session=lambda: "sess-2")
    denial = _denial()

    asyncio.run(mw._record("admin.teardown", denial, args={"force": True}))

    ((conn, event),) = events
    assert isinstance(conn, _FakeConn)
    assert event.principal == "alice"
    assert event.project == "demo"
    assert event.tool == "admin.teardown"
    assert event.agent_session == "sess-2"
    assert event.args == {"force": True}
    assert event.reason == str(denial)


def test_record_defaults_args_to_empty_dict(monkeypatch) -> None:
    events: list[Any] = []

    async def _record_denial(_conn: Any, *, event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(da.audit, "record_denial", _record_denial)
    mw = DenialAuditMiddleware(pool=_pool(_FakePool()), agent_session=lambda: None)

    asyncio.run(mw._record("admin.teardown", _denial()))

    assert events[0].args == {}


def test_default_agent_session_reads_request_context(monkeypatch) -> None:
    # the default agent_session callable resolves through the verified token
    monkeypatch.setattr(da, "request_context", lambda: SimpleNamespace(agent_session="sess-d"))
    events: list[Any] = []

    async def _record_denial(_conn: Any, *, event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(da.audit, "record_denial", _record_denial)
    mw = DenialAuditMiddleware(pool=_pool(_FakePool()))  # no agent_session override

    asyncio.run(mw._record("admin.teardown", _denial()))

    assert events[0].agent_session == "sess-d"
