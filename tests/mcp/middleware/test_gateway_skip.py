"""Purpose-keyed skip sets: which meta-tools skip per-call recording, and why (#866, #1654).

Two sets, two reasons (ADR-0485, amending ADR-0268 §6). ``REENTRANT_TOOLS`` is
de-duplication — ``tools.invoke`` dispatches through ``app.call_tool(run_middleware=True)``,
so the inner call is the authoritative record and the outer chain must add nothing.
``UNMETERED_TOOLS`` is volume — ``tools.search`` does *not* re-enter, so skipping it forgoes
the only record that would exist, taken deliberately for the usage plane alone.

These tests pin each middleware against the sets whose reason applies to it:

- ``UsageTrackingMiddleware`` records NOTHING for tools.invoke *or* tools.search.
- ``TelemetryMiddleware`` emits no span/metrics for tools.invoke, and **does** emit both for
  tools.search — the change #1654 made, and the one assertion here that would go quiet if
  ``tools.search`` were put back in the telemetry skip.
- ``DenialAuditMiddleware`` writes no denial row for tools.invoke, and **does** write one for
  tools.search.
- A gateway call writes exactly ONE usage row (keyed to inner tool, not tools.invoke).
- A denied gateway call writes exactly ONE denial-audit row (inner tool).

The denial-equivalence test verifies that the client-visible denial shape from a gateway
call is identical to a direct inner-tool denial.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.middleware import denial_audit as da_mod
from kdive.mcp.middleware.denial_audit import DenialAuditMiddleware
from kdive.mcp.middleware.shared import (
    REENTRANT_TOOLS,
    UNMETERED_TOOLS,
    result_error_category,
)
from kdive.mcp.middleware.usage import UsageTrackingMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, RoleDenied
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.usage_support import (
    denial_audit_must_not_fail,
    recording_must_not_fail,
    warm_pool,
)

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


def _pool(value: object) -> AsyncConnectionPool:
    return cast("AsyncConnectionPool", value)


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
# Skip-set constants
# ============================================================


def test_skip_sets_state_disjoint_reasons() -> None:
    """Each tool sits in exactly the set whose reason it satisfies, and in only one.

    Membership is pinned exactly, not by containment: the sets are small, closed, and the
    subject of ADR-0485, so a silent addition to either is a decision that was not recorded.
    Disjointness is asserted on top, because a name in both would mean the reason a given
    skip site implements is once again unrecoverable from the membership — exactly what let
    ADR-0268 §6's re-entry claim stand while being false of ``tools.search``.
    """
    assert sorted(REENTRANT_TOOLS) == ["tools.invoke"]
    assert sorted(UNMETERED_TOOLS) == ["tools.search"]
    assert not (REENTRANT_TOOLS & UNMETERED_TOOLS)


# ============================================================
# UsageTrackingMiddleware: skips both sets
# ============================================================


def _spy_usage() -> tuple[UsageTrackingMiddleware, list[Any]]:
    """UsageTrackingMiddleware with _record replaced by a spy."""
    mw = UsageTrackingMiddleware(pool=_pool(object()), secret_registry=SecretRegistry())
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
    """tools.search is skipped on the usage plane for volume, not de-duplication.

    It has no inner call and therefore no other recorder: this absent row is the only row
    that would ever exist, declined because a ``tool_invocation`` write on the highest-
    frequency agent call is not worth its signal (ADR-0485 §2). Contrast the telemetry test
    below, where the same tool *is* recorded.
    """
    mw, recorded = _spy_usage()
    result = ToolResponse.success("tools.search", "ok")

    asyncio.run(mw.on_call_tool(_context("tools.search"), _returning(result)))

    assert recorded == []


def test_usage_search_skips_recording_when_the_call_raises() -> None:
    """The usage skip covers the exception exit too: no row on any tools.search outcome.

    ADR-0485's "``tool_invocation`` contents are unchanged" is a claim about every exit, not
    just the returned-result one the success test above drives. ``UsageTrackingMiddleware``
    has three (return, ``AuthorizationError``, ``Exception``); this pins the third, which
    would otherwise record a row for a tool the decision says is never recorded.
    """
    mw, recorded = _spy_usage()

    with pytest.raises(RuntimeError):
        asyncio.run(mw.on_call_tool(_context("tools.search"), _raising(RuntimeError("boom"))))

    assert recorded == []


def test_usage_invoke_skips_recording_on_denial_envelope() -> None:
    """Outer tools.invoke result is authorization_denied — still no row."""
    mw, recorded = _spy_usage()
    denied = ToolResponse.failure("control.force_crash", ErrorCategory.AUTHORIZATION_DENIED)

    asyncio.run(mw.on_call_tool(_context("tools.invoke"), _returning(denied)))

    assert recorded == []


def test_usage_non_meta_tool_still_records() -> None:
    """A tool in neither skip set is unaffected by the guard."""
    mw, recorded = _spy_usage()
    result = ToolResponse.success("session.whoami", "ok")

    asyncio.run(mw.on_call_tool(_context("session.whoami"), _returning(result)))

    assert len(recorded) == 1


# ============================================================
# TelemetryMiddleware: skips REENTRANT_TOOLS only
# ============================================================


class _FakeMeter:
    """Records every counter add into one list, shared by the requests and errors counters.

    That sharing is load-bearing for the exact-list assertions below rather than merely
    tolerated: an error outcome appends *two* entries with distinct labels (``_finish``'s
    ``outcome="error"``, then ``_record_error``'s add carrying ``error_category``), so an
    equality check against a single ``outcome="ok"`` entry cannot be satisfied by an error
    path. A bare truthiness check on this list, by contrast, cannot tell the two apart.
    """

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


def test_telemetry_search_emits_span_and_metrics() -> None:
    """tools.search IS traced and metered: it never re-enters, so nothing else records it.

    The telemetry plane skips ``REENTRANT_TOOLS`` only (ADR-0485 §2). The span and its metric
    points stay in process — the span sampled besides — and this is the plane where discovery
    latency and error rate are answerable, unlike a ``tool_invocation`` row, which is a
    per-call Postgres write and stays declined.

    Every assertion below reddens if ``"tools.search"`` is added to the telemetry skip set:
    the skip is an early ``return await call_next(context)`` ahead of the span, so a skipped
    call leaves all three recorders empty.
    """
    mw, meter, tracer = _telemetry_mw()
    result = ToolResponse.success("tools.search", "ok")

    asyncio.run(mw.on_call_tool(_context("tools.search"), _returning(result)))

    labels = {"tool": "tools.search", "outcome": "ok"}
    assert tracer.span_names == ["mcp.tool/tools.search"]
    # Exactly the success counter: an error add would carry outcome="error" and mean the
    # middleware misclassified a successful envelope.
    assert meter.counter_adds == [(1, labels)]
    assert [lbl for _, lbl in meter.histogram_records] == [labels]


def test_telemetry_search_records_error_when_the_call_raises() -> None:
    """The error arm is instrumented for tools.search too, not just the success arm.

    ADR-0485 sells the telemetry skip's removal on discovery latency *and error rate*, and
    ``tools_search`` really can raise — it calls ``current_context()``, ``registered_tools``,
    ``tool_visible``, ``_rank`` and ``describe_tool`` outside any try. A skip narrowed later
    to the error path alone would leave the success test above green, so pin this exit
    separately: the ``REENTRANT_TOOLS`` check is one early return ahead of the span, but
    "one guard today" is not something a test should assume on the plane's behalf.

    The ``error_category`` label comes from the raised type, so a ``CategorizedError`` pins
    that it is the exception's own category rather than the infrastructure_failure default.
    """
    mw, meter, tracer = _telemetry_mw()
    boom = CategorizedError("registry unavailable", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    with pytest.raises(CategorizedError):
        asyncio.run(mw.on_call_tool(_context("tools.search"), _raising(boom)))

    assert tracer.span_names == ["mcp.tool/tools.search"]
    assert meter.counter_adds == [
        (1, {"tool": "tools.search", "outcome": "error"}),
        (
            1,
            {
                "tool": "tools.search",
                "outcome": "error",
                "error_category": ErrorCategory.INFRASTRUCTURE_FAILURE.value,
            },
        ),
    ]
    # The duration histogram too: `_finish` records it beside the requests counter, so a
    # regression that dropped it on the error arm alone would otherwise survive here.
    assert [lbl for _, lbl in meter.histogram_records] == [
        {"tool": "tools.search", "outcome": "error"}
    ]


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
# DenialAuditMiddleware: skips REENTRANT_TOOLS only
# ============================================================


def test_denial_audit_invoke_skips_row_on_role_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RoleDenied on tools.invoke: envelope is returned but NO denial row is written."""
    calls: list[Any] = []

    async def _mock_record_denial(_conn: Any, *, event: Any) -> None:
        calls.append(event)

    monkeypatch.setattr(da_mod.audit, "record_denial", _mock_record_denial)
    mw = DenialAuditMiddleware(pool=_pool(_FakePool()), agent_session=lambda: "sess-1")

    result = asyncio.run(mw.on_call_tool(_context("tools.invoke"), _raising(_role_denied())))

    # Still returns an authorization_denied envelope (outer chain depends on it)
    assert result_error_category(result) == ErrorCategory.AUTHORIZATION_DENIED.value
    # But writes no row
    assert calls == []


def test_denial_audit_search_writes_row_on_role_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RoleDenied on tools.search IS audited: only ``REENTRANT_TOOLS`` is skipped here.

    This pins the middleware's contract against the set, not a production reachability claim.
    ``tools.search`` cannot raise ``RoleDenied`` in production at all — it is in
    ``PUBLIC_TOOLS``, absent from ``_TOOL_SCOPES``, and ``tools_search`` calls no
    ``require_role``; it filters its own matches with ``tool_visible`` and returns them. Nor
    is the arm de-duplication: this middleware runs in the re-entered inner chain too, and
    the inner instance *returns* an enveloped denial rather than re-raising, so no exception
    can reach an outer instance to be audited twice (ADR-0485 §2). What the test pins is that
    the guard keys off the re-entrant set alone, so a tool that is not in it is audited.
    """
    calls: list[Any] = []

    async def _mock_record_denial(_conn: Any, *, event: Any) -> None:
        calls.append(event)

    monkeypatch.setattr(da_mod.audit, "record_denial", _mock_record_denial)
    mw = DenialAuditMiddleware(pool=_pool(_FakePool()), agent_session=lambda: "sess-1")

    result = asyncio.run(mw.on_call_tool(_context("tools.search"), _raising(_role_denied())))

    assert result_error_category(result) == ErrorCategory.AUTHORIZATION_DENIED.value
    assert [event.tool for event in calls] == ["tools.search"]


def test_denial_audit_non_meta_tool_still_writes_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool in neither skip set is unaffected by the guard."""
    calls: list[Any] = []

    async def _mock_record_denial(_conn: Any, *, event: Any) -> None:
        calls.append(event)

    monkeypatch.setattr(da_mod.audit, "record_denial", _mock_record_denial)
    mw = DenialAuditMiddleware(pool=_pool(_FakePool()), agent_session=lambda: "sess-1")

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
        async with warm_pool(migrated_url) as pool:
            mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry())

            inner_ctx = _Ctx("session.whoami")
            outer_ctx = _Ctx("tools.invoke")

            async def outer_call_next(_ctx: Any) -> ToolResponse:
                # Simulates the inner middleware chain recording for the inner tool.
                await mw.on_call_tool(inner_ctx, _returning(inner_ok))
                return inner_ok

            # Outer middleware processes tools.invoke; after the fix it records nothing.
            with recording_must_not_fail(), contextlib.suppress(Exception):
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
        async with warm_pool(migrated_url) as pool:
            usage_mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry())
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
            # Both guards outside the suppress — see tests.mcp.usage_support on nesting.
            with (
                recording_must_not_fail(),
                denial_audit_must_not_fail(),
                contextlib.suppress(Exception),
            ):
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
    direct_mw = DenialAuditMiddleware(pool=_pool(object()), agent_session=lambda: None)
    direct_mw._record = _noop_record  # ty: ignore[invalid-assignment]

    direct_result = asyncio.run(direct_mw.on_call_tool(_context(inner_tool), _raising(denial)))

    # --- Gateway call ---
    # Inner DenialAuditMW converts the denial to an envelope.
    inner_mw = DenialAuditMiddleware(pool=_pool(object()), agent_session=lambda: None)
    inner_mw._record = _noop_record  # ty: ignore[invalid-assignment]

    # Outer DenialAuditMW processes tools.invoke; it sees the envelope (no exception).
    outer_mw = DenialAuditMiddleware(pool=_pool(object()), agent_session=lambda: None)
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
