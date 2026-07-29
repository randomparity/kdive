"""Cover the authorization-denial audit middleware."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware import denial_audit as da
from kdive.mcp.middleware.denial_audit import (
    _DROP_ARGUMENT,
    DenialAuditMiddleware,
    _audit_args_from_message,
    _current_agent_session,
    _denied_result,
    _json_argument,
    _wrapped_denial,
)
from kdive.mcp.middleware.shared import result_error_category
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.errors import ProjectMembershipDenied
from kdive.security.authz.rbac import AuthorizationError, Role, RoleDenied


def _denial() -> RoleDenied:
    return RoleDenied(principal="alice", project="demo", held=Role.VIEWER, required=Role.OPERATOR)


def _wrapped(exc: BaseException) -> ToolError:
    """The exception exactly as FastMCP hands it to the chain (server.py:1358)."""
    try:
        raise ToolError("Error calling tool 'admin.teardown'") from exc
    except ToolError as wrapper:
        return wrapper


def _envelope(result: Any) -> ToolResponse:
    """The envelope carried by a middleware short-circuit, as the transport would read it."""
    assert isinstance(result, ToolResult), f"not transport-serializable: {result!r}"
    assert isinstance(result.structured_content, dict)
    return ToolResponse.model_validate(result.structured_content)


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
    # This middleware is the funnel for every `require_role` site that does not catch locally,
    # so the role it names here is what most of the surface reports (ADR-0490). `RoleDenied`
    # fires only past `require_role`'s membership check, so the caller is a member and the
    # required role discloses nothing their own membership did not.
    assert _envelope(result).data["missing_roles"] == [denial.required.value] == ["operator"]
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
    # The project is not granted at all: naming a role would confirm it exists (ADR-0490).
    assert "missing_roles" not in _envelope(result).data


# --- the ToolError-wrapped shape (ADR-0486) ---------------------------------


def test_on_call_tool_audits_role_denied_wrapped_in_tool_error() -> None:
    # The shape every real dispatch delivers: FastMCP demotes the denial to __cause__.
    mw, recorded = _spy_middleware()
    denial = _denial()

    async def call_next(_ctx: Any) -> ToolResponse:
        raise _wrapped(denial)

    result = asyncio.run(mw.on_call_tool(_context(arguments={"force": True}), call_next))

    assert result_error_category(result) == ErrorCategory.AUTHORIZATION_DENIED.value
    assert _envelope(result).data["missing_roles"] == ["operator"]
    (tool, recorded_denial, args) = recorded[0]
    assert (tool, args) == ("admin.teardown", {"force": True})
    # The audited denial is the wrapped original, so its principal/project/reason are the real
    # ones — not a value reconstructed from the wrapper's message.
    assert recorded_denial is denial


def test_on_call_tool_envelopes_wrapped_project_membership_denied() -> None:
    mw, recorded = _spy_middleware()

    async def call_next(_ctx: Any) -> ToolResponse:
        raise _wrapped(ProjectMembershipDenied("not a member"))

    result = asyncio.run(mw.on_call_tool(_context(name="runs.create"), call_next))

    assert result_error_category(result) == ErrorCategory.AUTHORIZATION_DENIED.value
    assert recorded == []
    assert "missing_roles" not in _envelope(result).data


def test_on_call_tool_reraises_tool_error_over_an_ordinary_failure() -> None:
    # The unwrap must not turn every wrapped exception into a denial: a ToolError whose cause
    # is an unrelated error keeps propagating, unchanged and unaudited.
    mw, recorded = _spy_middleware()
    wrapper = _wrapped(RuntimeError("disk on fire"))

    async def call_next(_ctx: Any) -> ToolResponse:
        raise wrapper

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(mw.on_call_tool(_context(), call_next))
    assert excinfo.value is wrapper
    assert recorded == []


def test_on_call_tool_reraises_tool_error_over_a_non_role_authorization_error() -> None:
    # `DestructiveOpDenied` and `require_platform_role` denials are audited by their own
    # handlers; sweeping them in here would double-write (ADR-0062 §5).
    mw, recorded = _spy_middleware()
    wrapper = _wrapped(AuthorizationError("destructive op refused"))

    async def call_next(_ctx: Any) -> ToolResponse:
        raise wrapper

    with pytest.raises(ToolError):
        asyncio.run(mw.on_call_tool(_context(), call_next))
    assert recorded == []


def test_on_call_tool_reraises_bare_tool_error() -> None:
    # A ToolError raised with no `from` has __cause__ None; the isinstance check must not
    # mistake that for a denial.
    mw, _recorded = _spy_middleware()
    bare = ToolError("no cause at all")

    async def call_next(_ctx: Any) -> ToolResponse:
        raise bare

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(mw.on_call_tool(_context(), call_next))
    assert excinfo.value is bare


def test_wrapped_denial_ignores_a_denial_reachable_only_through_a_second_hop() -> None:
    # The unwrap reads the *immediate* cause only, like gateway.py and binding_errors.py. A
    # handler that deliberately converted a denial (`raise Other from denial`) keeps its
    # conversion rather than having the denial resurrected underneath it (ADR-0486).
    conversion = RuntimeError("converted")
    conversion.__cause__ = _denial()
    assert _wrapped_denial(_wrapped(conversion)) is None


def test_denied_result_is_serializable_by_the_transport() -> None:
    # `_call_tool_mcp` calls `result.to_mcp_result()`; a bare ToolResponse has no such method,
    # so an unwrapped envelope reaches the client as an AttributeError (ADR-0486).
    mcp_result = _denied_result("admin.teardown", missing=_denial()).to_mcp_result()
    assert isinstance(mcp_result, tuple), f"no structured content to serialize: {mcp_result!r}"
    structured = mcp_result[1]
    assert structured["error_category"] == ErrorCategory.AUTHORIZATION_DENIED.value
    assert structured["data"]["missing_roles"] == ["operator"]


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
