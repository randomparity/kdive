"""Per-call usage recording + outcome classification (#506, ADR-0148).

The middleware records one best-effort ``tool_invocation`` row per call. Outcome covers
every denial path: the ``authorization_denied`` envelope ``DenialAuditMiddleware`` returns
(a ``ToolResult`` on the normal path, a bare ``ToolResponse`` on its short-circuit) *and* a
propagated ``AuthorizationError`` (``DestructiveOpDenied`` / non-member) that bubbles past
it. A recording failure is swallowed — it never fails the call.

Because that swallow is the middleware's contract, every row-asserting test drives it
over a warm pool and under a guard that names a swallowed failure — see
``tests.mcp.usage_support`` for both, and for why (#1527).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
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
from tests.mcp.usage_support import recording_must_not_fail, warm_pool


def _ctx() -> RequestContext:
    return RequestContext(
        principal="alice", agent_session="s1", projects=("a",), roles={"a": Role.OPERATOR}
    )


class _Ctx:
    def __init__(self, tool: str) -> None:
        self.message = type("M", (), {"name": tool, "arguments": {"project": "a"}})()


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
        async with warm_pool(migrated_url) as pool:
            mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry())
            with recording_must_not_fail(), contextlib.suppress(Exception):
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
        async with warm_pool(migrated_url) as pool:
            mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry())
            with recording_must_not_fail():
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

        with recording_must_not_fail():
            await mw.on_call_tool(_Ctx("jobs.get"), ok)

    with pytest.raises(AssertionError, match="PoolClosed"):
        asyncio.run(_run())


def test_warm_pool_has_a_connection_before_the_caller_acquires(migrated_url: str) -> None:
    # This is the whole of the #1527 fix: the connect must land *outside* the
    # middleware's 1-second acquire budget. Pin it directly — every other test here
    # passes just as well over a cold pool on an idle machine, so without this one a
    # refactor back to the psycopg default would reopen the flake silently.
    async def _run() -> int:
        async with warm_pool(migrated_url) as pool:
            return pool.get_stats()["pool_available"]

    assert asyncio.run(_run()) >= 1  # 0 if the pool were opened with wait=False


def test_recording_guard_ignores_unrelated_warnings() -> None:
    # Only the recording-failure warning may trip the guard; anything else this module
    # logs must not be misreported as a lost usage row.
    with recording_must_not_fail():
        logging.getLogger(UsageTrackingMiddleware.__module__).warning("slow write, not a failure")
