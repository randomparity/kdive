"""Per-call usage recording + outcome classification (#506, ADR-0148).

The middleware records one best-effort ``tool_invocation`` row per call. Outcome covers
every denial path: the ``authorization_denied`` envelope ``DenialAuditMiddleware`` returns
(a ``ToolResult`` on the normal path, a bare ``ToolResponse`` on its short-circuit) *and* a
propagated ``AuthorizationError`` (``DestructiveOpDenied`` / non-member) that bubbles past
it. A recording failure is swallowed — it never fails the call.

Because that swallow is the middleware's contract, every test here drives it over a
*warm* pool: ``_open_warm_pool`` establishes the connection before the middleware's
1-second acquire budget starts, so a saturated machine cannot turn a slow first connect
into a missing row (#1527). ``_recording_must_not_fail`` names the swallowed cause if it
ever happens anyway, instead of leaving a bare ``IndexError`` on the row assertions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from fastmcp.tools.base import ToolResult
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.usage import UsageTrackingMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.context import RequestContext
from kdive.security.authz.gate import DestructiveOpDenied
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry


def _ctx() -> RequestContext:
    return RequestContext(
        principal="alice", agent_session="s1", projects=("a",), roles={"a": Role.OPERATOR}
    )


class _Ctx:
    def __init__(self, tool: str) -> None:
        self.message = type("M", (), {"name": tool, "arguments": {"project": "a"}})()


_RECORDING_FAILURE = "usage recording failed"


class _Capture(logging.Handler):
    """Collect the middleware's recording-failure warnings, tracebacks included.

    ``_record`` logs its one warning with ``exc_info=True``, and the swallowed exception
    is the whole diagnostic value — ``record.getMessage()`` alone yields only the tool
    name. ``Formatter.format`` appends the traceback whenever ``exc_info`` is set, so it
    is what preserves the cause. Only this module's recording-failure warning is
    collected: an unrelated future warning on the same logger must not be reported as a
    recording failure.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.failures: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith(_RECORDING_FAILURE):
            self.failures.append(logging.Formatter().format(record))


@contextlib.contextmanager
def _recording_must_not_fail() -> Iterator[None]:
    """Turn the middleware's swallowed recording failure into a named assertion.

    ``UsageTrackingMiddleware._record`` logs and swallows every failure so a bad
    recording can never fail the tool call. In a test that then asserts on the recorded
    row, a swallowed failure surfaces only as an empty result set — the bare
    ``IndexError`` reported in #1527, which says nothing about the cause. Re-raise the
    captured warning, traceback and all, instead.

    The check runs in the ``finally`` so a raising body cannot discard it; Python chains
    the original exception onto the ``AssertionError`` as its context.
    """
    logger = logging.getLogger(UsageTrackingMiddleware.__module__)
    handler = _Capture()
    previous = logger.level
    logger.setLevel(logging.WARNING)  # the logger's own level, not the global disable floor
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        if handler.failures:
            raise AssertionError(handler.failures[0])


async def _open_warm_pool(url: str) -> AsyncConnectionPool:
    """A pool with its connection already established.

    ``open()`` defaults to ``wait=False``, which returns before any connection exists and
    leaves the first connect to a background task. The middleware then acquires with a
    1-second budget (its production default), so on a machine saturated by the suite's
    ``-n auto --dist worksteal`` run that first connect can exceed the budget, raise
    ``PoolTimeout``, and be swallowed — leaving no row (#1527). Waiting here moves the
    connect outside the budget, and makes a genuinely unreachable backend fail loudly at
    open time rather than as a silently missing row.
    """
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open(wait=True)
    return pool


def _drive(
    migrated_url: str,
    tool: str,
    behavior: Callable[[Any], Awaitable[Any]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    cli_client_id: str = "cli-x",
) -> list[tuple[Any, ...]]:
    """Run the middleware over ``behavior``; return the recorded rows."""
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", _ctx)
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", cli_client_id)

    async def _run() -> list[tuple[Any, ...]]:
        async with await _open_warm_pool(migrated_url) as pool:
            mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry())
            with _recording_must_not_fail(), contextlib.suppress(Exception):
                await mw.on_call_tool(_Ctx(tool), behavior)
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT tool, outcome, principal, project FROM tool_invocation"
                )
                return await cur.fetchall()

    return asyncio.run(_run())


def test_args_digest_present_on_recorded_row(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every recorded row carries a redacted-args digest, computed on the success path too.
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", _ctx)
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "cli-x")

    async def ok(_c: Any) -> ToolResult:
        envelope = ToolResponse.success("jobs.get", "ok")
        return ToolResult(structured_content=envelope.model_dump(mode="json"))

    async def _run() -> str | None:
        async with await _open_warm_pool(migrated_url) as pool:
            mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry())
            with _recording_must_not_fail():
                await mw.on_call_tool(_Ctx("jobs.get"), ok)
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT args_digest FROM tool_invocation")
                row = await cur.fetchone()
        assert row is not None
        return row[0]

    digest = asyncio.run(_run())
    assert isinstance(digest, str) and len(digest) == 64


def test_ok_outcome_from_toolresult(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(_c: Any) -> ToolResult:
        envelope = ToolResponse.success("jobs.get", "ok")
        return ToolResult(structured_content=envelope.model_dump(mode="json"))

    rows = _drive(migrated_url, "jobs.get", ok, monkeypatch)
    assert rows == [("jobs.get", "ok", "alice", "a")]


def test_denied_from_envelope(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def denied(_c: Any) -> ToolResult:
        envelope = ToolResponse.failure("x", ErrorCategory.AUTHORIZATION_DENIED)
        return ToolResult(structured_content=envelope.model_dump(mode="json"))

    rows = _drive(migrated_url, "x", denied, monkeypatch)
    assert rows[0][1] == "denied"


def test_denied_from_bare_toolresponse(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # DenialAuditMiddleware short-circuits with a bare ToolResponse, not a ToolResult.
    async def denied(_c: Any) -> ToolResponse:
        return ToolResponse.failure("x", ErrorCategory.AUTHORIZATION_DENIED)

    rows = _drive(migrated_url, "x", denied, monkeypatch)
    assert rows[0][1] == "denied"


def test_denied_from_propagated_authorization_error(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_c: Any) -> Any:
        raise DestructiveOpDenied(["admin_role"])

    rows = _drive(migrated_url, "control.force_crash", boom, monkeypatch)
    assert rows[0][1] == "denied"


def test_error_from_failure_envelope(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def err(_c: Any) -> ToolResult:
        envelope = ToolResponse.failure("y", ErrorCategory.INFRASTRUCTURE_FAILURE)
        return ToolResult(structured_content=envelope.model_dump(mode="json"))

    rows = _drive(migrated_url, "y", err, monkeypatch)
    assert rows[0][1] == "error"


def test_error_from_propagated_exception(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_c: Any) -> Any:
        raise RuntimeError("kaboom")

    rows = _drive(migrated_url, "z", boom, monkeypatch)
    assert rows[0][1] == "error"


def test_recording_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A never-opened pool makes recording fail; the success result must still return.
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", _ctx)
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "cli-x")

    async def _run() -> Any:
        pool = AsyncConnectionPool("postgresql://unused", open=False)
        mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry(), acquire_timeout=0.05)

        async def ok(_c: Any) -> ToolResult:
            envelope = ToolResponse.success("jobs.get", "ok")
            return ToolResult(structured_content=envelope.model_dump(mode="json"))

        return await mw.on_call_tool(_Ctx("jobs.get"), ok)

    result = asyncio.run(_run())
    assert result is not None  # the call result is unaffected by the recording failure


def test_swallowed_recording_failure_names_its_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    # The guard the row-asserting tests wrap their drive in must actually bite, and must
    # report *why* recording failed. "usage recording failed for tool jobs.get" alone is
    # no more diagnostic than the IndexError it replaces (#1527), so drive a real
    # swallowed failure and require the swallowed exception in the message.
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", _ctx)
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "cli-x")

    async def _run() -> None:
        pool = AsyncConnectionPool("postgresql://unused", open=False)  # never opened
        mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry(), acquire_timeout=0.05)

        async def ok(_c: Any) -> ToolResult:
            envelope = ToolResponse.success("jobs.get", "ok")
            return ToolResult(structured_content=envelope.model_dump(mode="json"))

        with _recording_must_not_fail():
            await mw.on_call_tool(_Ctx("jobs.get"), ok)

    with pytest.raises(AssertionError, match="PoolClosed"):
        asyncio.run(_run())


def test_recording_guard_ignores_unrelated_warnings() -> None:
    # Only the recording-failure warning may trip the guard; anything else this module
    # logs must not be misreported as a lost usage row.
    with _recording_must_not_fail():
        logging.getLogger(UsageTrackingMiddleware.__module__).warning("slow write, not a failure")
