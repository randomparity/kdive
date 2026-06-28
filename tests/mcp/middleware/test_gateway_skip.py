"""META_TOOLS skip-set: meta-tools skip per-call recording/audit (#866, Task 6).

Failing tests (before the fix) verify:
- UsageTrackingMiddleware records NOTHING for tools.invoke / tools.search.
- TelemetryMiddleware emits NO span or metrics for tools.invoke / tools.search.
- DenialAuditMiddleware writes NO denial row for tools.invoke / tools.search.
- A gateway call writes exactly ONE usage row (keyed to inner tool, not tools.invoke).
- A denied gateway call writes exactly ONE denial-audit row (inner tool).

The denial-equivalence test verifies (before and after the fix) that the client-visible
denial shape from a gateway call is identical to a direct inner-tool denial.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware import denial_audit as da_mod
from kdive.mcp.middleware.denial_audit import DenialAuditMiddleware
from kdive.mcp.middleware.shared import META_TOOLS, result_error_category
from kdive.mcp.middleware.usage import UsageTrackingMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, RoleDenied

# ============================================================
# Shared helpers
# ============================================================


def _context(name: str, arguments: Any = None) -> Any:
    return SimpleNamespace(message=SimpleNamespace(name=name, arguments=arguments))


def _role_denied() -> RoleDenied:
    return RoleDenied(principal="alice", project="proj", held=Role.VIEWER, required=Role.OPERATOR)


def _request_ctx() -> RequestContext:
    return RequestContext(
        principal="alice",
        agent_session="sess-1",
        projects=("proj",),
        roles={"proj": Role.VIEWER},
    )


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


class _Ctx:
    """Minimal middleware context for UsageTrackingMiddleware."""

    def __init__(self, tool: str) -> None:
        self.message = type("M", (), {"name": tool, "arguments": {"project": "proj"}})()


def _returning(value: Any) -> Any:
    """Return an async callable that yields ``value`` when awaited."""

    async def _call_next(_ctx: Any) -> Any:
        return value

    return _call_next


def _raising(exc: BaseException) -> Any:
    """Return an async callable that raises ``exc``."""

    async def _call_next(_ctx: Any) -> Any:
        raise exc

    return _call_next


# ============================================================
# META_TOOLS constant
# ============================================================


def test_meta_tools_contains_invoke_and_search() -> None:
    assert "tools.invoke" in META_TOOLS
    assert "tools.search" in META_TOOLS


# ============================================================
# UsageTrackingMiddleware: skip for meta-tools
# ============================================================


def _spy_usage() -> tuple[UsageTrackingMiddleware, list[Any]]:
    """UsageTrackingMiddleware with _record replaced by a spy."""
    mw = UsageTrackingMiddleware(pool=object())
    recorded: list[Any] = []

    async def _record(ctx: Any, outcome: Any) -> None:
        recorded.append((ctx, outcome))

    mw._record = _record  # ty: ignore[invalid-assignment]
    return mw, recorded


def test_usage_invoke_skips_recording_on_success() -> None:
    mw, recorded = _spy_usage()
    result = ToolResponse.success("session.whoami", "ok")

    asyncio.run(mw.on_call_tool(_context("tools.invoke"), _returning(result)))

    assert recorded == []


def test_usage_search_skips_recording_on_success() -> None:
    mw, recorded = _spy_usage()
    result = ToolResponse.success("tools.search", "ok")

    asyncio.run(mw.on_call_tool(_context("tools.search"), _returning(result)))

    assert recorded == []


def test_usage_invoke_skips_recording_on_denial_envelope() -> None:
    """Outer tools.invoke result is authorization_denied — still no row."""
    mw, recorded = _spy_usage()
    denied = ToolResponse.failure("control.force_crash", ErrorCategory.AUTHORIZATION_DENIED)

    asyncio.run(mw.on_call_tool(_context("tools.invoke"), _returning(denied)))

    assert recorded == []


def test_usage_non_meta_tool_still_records() -> None:
    """Regular tools are unaffected by the meta-tool guard."""
    mw, recorded = _spy_usage()
    result = ToolResponse.success("session.whoami", "ok")

    asyncio.run(mw.on_call_tool(_context("session.whoami"), _returning(result)))

    assert len(recorded) == 1


# ============================================================
# TelemetryMiddleware: skip for meta-tools
# ============================================================


class _FakeMeter:
    def __init__(self) -> None:
        self.counter_adds: list[tuple[int, dict[str, str]]] = []
        self.histogram_records: list[tuple[float, dict[str, str]]] = []

    def create_counter(self, name: str, **_: Any) -> _FakeCounter:
        return _FakeCounter(self.counter_adds)

    def create_histogram(self, name: str, **_: Any) -> _FakeHistogram:
        return _FakeHistogram(self.histogram_records)


class _FakeCounter:
    def __init__(self, calls: list[tuple[int, dict[str, str]]]) -> None:
        self._calls = calls

    def add(self, amount: int, labels: dict[str, str]) -> None:
        self._calls.append((amount, labels))


class _FakeHistogram:
    def __init__(self, calls: list[tuple[float, dict[str, str]]]) -> None:
        self._calls = calls

    def record(self, value: float, labels: dict[str, str]) -> None:
        self._calls.append((value, labels))


class _FakeTracer:
    def __init__(self) -> None:
        self.span_names: list[str] = []

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, **_: Any) -> Any:
        self.span_names.append(name)
        yield _FakeSpan()


class _FakeSpan:
    def set_attribute(self, *_: Any) -> None:
        pass

    def set_status(self, *_: Any) -> None:
        pass

    def record_exception(self, *_: Any) -> None:
        pass


def _telemetry_mw() -> tuple[Any, _FakeMeter, _FakeTracer]:
    from kdive.mcp.middleware.telemetry import TelemetryMiddleware

    meter = _FakeMeter()
    tracer = _FakeTracer()
    return TelemetryMiddleware(tracer=tracer, meter=meter), meter, tracer


def test_telemetry_invoke_emits_no_span_or_metric() -> None:
    mw, meter, tracer = _telemetry_mw()
    result = ToolResponse.success("session.whoami", "ok")

    asyncio.run(mw.on_call_tool(_context("tools.invoke"), _returning(result)))

    assert tracer.span_names == []
    assert meter.counter_adds == []
    assert meter.histogram_records == []


def test_telemetry_search_emits_no_span_or_metric() -> None:
    mw, meter, tracer = _telemetry_mw()
    result = ToolResponse.success("tools.search", "ok")

    asyncio.run(mw.on_call_tool(_context("tools.search"), _returning(result)))

    assert tracer.span_names == []
    assert meter.counter_adds == []
    assert meter.histogram_records == []


def test_telemetry_invoke_still_passes_through_to_call_next() -> None:
    """Skipping telemetry still forwards context to call_next and returns the result."""
    mw, _, _ = _telemetry_mw()
    sentinel = ToolResponse.success("session.whoami", "ok")

    result = asyncio.run(mw.on_call_tool(_context("tools.invoke"), _returning(sentinel)))

    assert result is sentinel


def test_telemetry_non_meta_tool_still_emits_span() -> None:
    mw, meter, tracer = _telemetry_mw()
    result = ToolResponse.success("session.whoami", "ok")

    asyncio.run(mw.on_call_tool(_context("session.whoami"), _returning(result)))

    assert tracer.span_names == ["mcp.tool/session.whoami"]
    assert meter.counter_adds  # at least one counter add


# ============================================================
# DenialAuditMiddleware: skip row for meta-tools
# ============================================================


def test_denial_audit_invoke_skips_row_on_role_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RoleDenied on tools.invoke: envelope is returned but NO denial row is written."""
    calls: list[Any] = []

    async def _mock_record_denial(_conn: Any, *, event: Any) -> None:
        calls.append(event)

    monkeypatch.setattr(da_mod.audit, "record_denial", _mock_record_denial)
    mw = DenialAuditMiddleware(pool=_FakePool(), agent_session=lambda: "sess-1")

    result = asyncio.run(mw.on_call_tool(_context("tools.invoke"), _raising(_role_denied())))

    # Still returns an authorization_denied envelope (outer chain depends on it)
    assert result_error_category(result) == ErrorCategory.AUTHORIZATION_DENIED.value
    # But writes no row
    assert calls == []


def test_denial_audit_non_meta_tool_still_writes_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-meta-tools are unaffected by the guard."""
    calls: list[Any] = []

    async def _mock_record_denial(_conn: Any, *, event: Any) -> None:
        calls.append(event)

    monkeypatch.setattr(da_mod.audit, "record_denial", _mock_record_denial)
    mw = DenialAuditMiddleware(pool=_FakePool(), agent_session=lambda: "sess-1")

    result = asyncio.run(mw.on_call_tool(_context("control.force_crash"), _raising(_role_denied())))

    assert result_error_category(result) == ErrorCategory.AUTHORIZATION_DENIED.value
    assert len(calls) == 1
    assert calls[0].tool == "control.force_crash"


# ============================================================
# Integration: gateway call writes exactly one usage row (inner tool)
# ============================================================


def test_invoke_writes_one_usage_row_keyed_to_inner(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gateway call writes exactly one tool_invocation row, keyed to the inner tool."""
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", _request_ctx)
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "cli-x")

    inner_ok = ToolResponse.success("session.whoami", "ok")

    async def _run() -> list[tuple[Any, ...]]:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            mw = UsageTrackingMiddleware(pool)

            inner_ctx = _Ctx("session.whoami")
            outer_ctx = _Ctx("tools.invoke")

            async def outer_call_next(_ctx: Any) -> ToolResponse:
                # Simulates the inner middleware chain recording for the inner tool.
                await mw.on_call_tool(inner_ctx, _returning(inner_ok))
                return inner_ok

            # Outer middleware processes tools.invoke; after the fix it records nothing.
            with contextlib.suppress(Exception):
                await mw.on_call_tool(outer_ctx, outer_call_next)

            async with pool.connection() as conn:
                cur = await conn.execute("SELECT tool, outcome FROM tool_invocation ORDER BY ts")
                return await cur.fetchall()

    rows = asyncio.run(_run())
    assert rows == [("session.whoami", "ok")]


# ============================================================
# Integration: denied gateway call writes exactly one denial row (inner tool)
# ============================================================


def test_denied_invoke_writes_one_denial_row_keyed_to_inner(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Denied gateway call: exactly one denial-audit row and one usage row, both inner-keyed."""
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", _request_ctx)
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "cli-x")

    denial = _role_denied()

    async def _run() -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            usage_mw = UsageTrackingMiddleware(pool)
            denial_mw = DenialAuditMiddleware(pool, agent_session=lambda: "sess-1")

            inner_ctx = _Ctx("control.force_crash")
            outer_ctx = _Ctx("tools.invoke")

            async def outer_call_next(_ctx: Any) -> Any:
                # Inner DenialAuditMiddleware catches RoleDenied, writes ONE denial row,
                # returns an authorization_denied envelope.
                inner_result = await denial_mw.on_call_tool(inner_ctx, _raising(denial))
                # Inner UsageTrackingMiddleware records ONE usage row for inner tool.
                await usage_mw.on_call_tool(inner_ctx, _returning(inner_result))
                return inner_result

            # Outer usage middleware processes tools.invoke.
            # After the fix: skips recording for tools.invoke.
            with contextlib.suppress(Exception):
                await usage_mw.on_call_tool(outer_ctx, outer_call_next)

            async with pool.connection() as conn:
                cur = await conn.execute("SELECT tool FROM audit_log ORDER BY ts")
                denial_rows = await cur.fetchall()
                cur2 = await conn.execute("SELECT tool, outcome FROM tool_invocation ORDER BY ts")
                usage_rows = await cur2.fetchall()

            return denial_rows, usage_rows

    denial_rows, usage_rows = asyncio.run(_run())

    # Exactly one denial-audit row, keyed to inner tool
    assert denial_rows == [("control.force_crash",)]

    # Exactly one usage row, keyed to inner tool, outcome=denied
    assert usage_rows == [("control.force_crash", "denied")]


# ============================================================
# Denial equivalence: direct call vs gateway call produce same client shape
# ============================================================


def test_denial_equivalence_direct_vs_gateway() -> None:
    """Client-visible denial shape is identical whether via direct call or gateway.

    Direct path: DenialAuditMW sees RoleDenied for the inner tool, converts to envelope.
    Gateway path: outer DenialAuditMW sees the envelope from the inner chain (no exception
    raised at the outer level), passes it through unchanged.

    Both yield error_category=authorization_denied keyed to the inner tool name.
    """
    denial = _role_denied()
    inner_tool = "control.force_crash"

    async def _noop_record(*_a: Any, **_k: Any) -> None:
        pass

    # --- Direct call ---
    direct_mw = DenialAuditMiddleware(pool=object(), agent_session=lambda: None)
    direct_mw._record = _noop_record  # ty: ignore[invalid-assignment]

    direct_result = asyncio.run(direct_mw.on_call_tool(_context(inner_tool), _raising(denial)))

    # --- Gateway call ---
    # Inner DenialAuditMW converts the denial to an envelope.
    inner_mw = DenialAuditMiddleware(pool=object(), agent_session=lambda: None)
    inner_mw._record = _noop_record  # ty: ignore[invalid-assignment]

    # Outer DenialAuditMW processes tools.invoke; it sees the envelope (no exception).
    outer_mw = DenialAuditMiddleware(pool=object(), agent_session=lambda: None)
    outer_mw._record = _noop_record  # ty: ignore[invalid-assignment]

    async def _gateway() -> Any:
        inner_result = await inner_mw.on_call_tool(_context(inner_tool), _raising(denial))
        # tools.invoke passes the envelope to the outer chain unchanged
        return await outer_mw.on_call_tool(_context("tools.invoke"), _returning(inner_result))

    gateway_result = asyncio.run(_gateway())

    # Same error category
    assert result_error_category(direct_result) == ErrorCategory.AUTHORIZATION_DENIED.value
    assert result_error_category(gateway_result) == ErrorCategory.AUTHORIZATION_DENIED.value

    # Same object_id — keyed to the inner tool, NOT to tools.invoke
    assert isinstance(direct_result, ToolResponse)
    assert isinstance(gateway_result, ToolResponse)
    assert direct_result.object_id == inner_tool
    assert gateway_result.object_id == inner_tool
