"""Per-call usage recording + outcome classification (#506, ADR-0148).

The middleware records one best-effort ``tool_invocation`` row per call. Every denial reaches
it as a **returned** ``authorization_denied`` envelope — built by the tool's own handler, or by
``DenialAuditMiddleware`` (ADR-0486) — and is classified from that envelope. A recording
failure is swallowed; it never fails the call.

Two claims this docstring used to make are gone, and neither is coming back:

* *a propagated ``AuthorizationError`` that bubbles past the boundary.* FastMCP builds the
  middleware chain outside the branch that wraps a tool exception in ``ToolError``, so no
  denial can arrive here unwrapped. That arm was deleted in ADR-0493, along with the direct-
  ``on_call_tool`` test that "covered" it — the shape ADR-0486 §4 disqualifies, which stayed
  green through the whole #1635 regression. The one class that really did escape its handler,
  ``accounting.report``'s non-member ``AuthorizationError``, is fixed at the handler and pinned
  by a real-dispatch test in ``tests/mcp/tools/test_gateway_usage_recording_e2e.py``.
* *a bare ``ToolResponse`` on the boundary's short-circuit.* ADR-0486 decision 2 deleted that
  short-circuit; the boundary returns a ``ToolResult``. ``test_denied_from_bare_toolresponse``
  below survives it because ``result_error_category`` is a shared helper that accepts both
  shapes, not because anything still produces the bare one.

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

import kdive.mcp.middleware.usage as usage_mod
from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.usage import UsageTrackingMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.usage_support import recording_must_not_fail, warm_pool

# A loopback port nothing listens on: connects are refused immediately, so an opened pool
# retries and the acquire exhausts its budget rather than hanging on an unroutable address.
_UNREACHABLE_DB = "postgresql://kdive@127.0.0.1:1/kdive?connect_timeout=1"


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
    # `result_error_category` accepts a bare ToolResponse as well as a ToolResult, so the
    # classifier does too. Nothing on the dispatch path produces the bare shape any more —
    # ADR-0486 decision 2 removed the short-circuit that did — so this pins the shared helper's
    # tolerance, not a live wire format. See the module docstring.
    async def denied(_c: Any) -> ToolResponse:
        return ToolResponse.failure("x", ErrorCategory.AUTHORIZATION_DENIED)

    rows = _drive(migrated_url, "x", denied, monkeypatch)
    assert rows[0][1] == "denied"


# `test_denied_from_propagated_authorization_error` used to sit here, raising `DestructiveOpDenied`
# from `call_next` and asserting `denied`. ADR-0493 deleted the `except AuthorizationError` arm it
# depended on: FastMCP never delivers an unwrapped denial to this middleware, so the test only ever
# proved that a hand-driven `on_call_tool` behaves as written. `DestructiveOpDenied` has exactly one
# raiser (`security/authz/gate.py`) and one catcher (`lifecycle/control/registrar.py`), which
# envelopes it before it can reach any middleware.


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


def test_swallowed_recording_failure_increments_the_drop_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0148 keeps the swallow, ADR-0449 makes it countable: a dropped row is only
    # observable to an operator through a WARNING in the log stream today, which is
    # invisible to anything scraping /metrics.
    #
    # An *open* pool against an unreachable backend, not the never-opened pool the tests
    # above use: that one raises PoolClosed, whereas the condition #1535 is about — and the
    # one the `pool_timeout` label has to isolate — is an open pool that cannot hand out a
    # connection inside the acquire budget.
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", _ctx)
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "cli-x")
    drops: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(
        usage_mod._RECORDING_FAILURES,
        "add",
        lambda amount, labels: drops.append((amount, labels)),
    )

    async def _run() -> None:
        pool = AsyncConnectionPool(_UNREACHABLE_DB, min_size=1, open=False)
        await pool.open()  # cold by default: no connection exists to hand out
        mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry(), acquire_timeout=0.05)

        async def ok(_c: Any) -> ToolResult:
            envelope = ToolResponse.success("jobs.get", "ok")
            return ToolResult(structured_content=envelope.model_dump(mode="json"))

        try:
            await mw.on_call_tool(_Ctx("jobs.get"), ok)
        finally:
            await pool.close(timeout=1.0)

    asyncio.run(_run())
    # `pool_timeout` specifically: the whole point of the label is that a pool that could not
    # hand out a connection — the loss mode ADR-0449 defers pool sizing to — is separable from
    # a SQL or schema failure, which argues for something else entirely.
    assert drops == [(1, {"reason": "pool_timeout"})]


def test_a_non_pool_recording_failure_counts_as_other(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other half of the label's job. `_record`'s except clause wraps the whole body, so a
    # failure that is *not* the pool failing to hand out a connection must not read as
    # evidence for a pool-sizing change — that is the decision ADR-0449 defers to this series.
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", _ctx)
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "cli-x")
    drops: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(
        usage_mod._RECORDING_FAILURES,
        "add",
        lambda amount, labels: drops.append((amount, labels)),
    )

    def _broken_insert(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError('relation "tool_invocation" does not exist')

    monkeypatch.setattr(usage_mod, "record_usage", _broken_insert)

    async def _run() -> None:
        async with warm_pool(migrated_url) as pool:
            mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry())

            async def ok(_c: Any) -> ToolResult:
                envelope = ToolResponse.success("jobs.get", "ok")
                return ToolResult(structured_content=envelope.model_dump(mode="json"))

            await mw.on_call_tool(_Ctx("jobs.get"), ok)

    asyncio.run(_run())
    assert drops == [(1, {"reason": "other"})]


def test_a_failing_drop_counter_still_cannot_fail_the_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0148's swallow is unconditional, so the instrument added to observe it must not
    # outrank the failure it observes: a raising `add()` must not escape `_record` and fail
    # a call the swallow exists to protect.
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", _ctx)
    monkeypatch.setenv("KDIVE_CLI_CLIENT_ID", "cli-x")

    def _broken(_amount: int, _labels: dict[str, str]) -> None:
        raise RuntimeError("meter provider is broken")

    monkeypatch.setattr(usage_mod._RECORDING_FAILURES, "add", _broken)

    async def _run() -> Any:
        pool = AsyncConnectionPool("postgresql://unused", open=False)  # never opened
        mw = UsageTrackingMiddleware(pool, secret_registry=SecretRegistry(), acquire_timeout=0.05)

        async def ok(_c: Any) -> ToolResult:
            envelope = ToolResponse.success("jobs.get", "ok")
            return ToolResult(structured_content=envelope.model_dump(mode="json"))

        return await mw.on_call_tool(_Ctx("jobs.get"), ok)

    assert asyncio.run(_run()) is not None


def test_successful_recording_leaves_the_drop_counter_alone(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The counter must mean "a row was lost", not "a call happened" — otherwise an alert on
    # it fires constantly and stops being read.
    drops: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(
        usage_mod._RECORDING_FAILURES,
        "add",
        lambda amount, labels: drops.append((amount, labels)),
    )

    async def ok(_c: Any) -> ToolResult:
        envelope = ToolResponse.success("jobs.get", "ok")
        return ToolResult(structured_content=envelope.model_dump(mode="json"))

    assert _drive(migrated_url, "jobs.get", ok, monkeypatch) == [("jobs.get", "ok", "alice", "a")]
    assert drops == []


def test_warm_pool_has_a_connection_before_the_caller_acquires(migrated_url: str) -> None:
    # This is the whole of the #1527 fix: the connect must land *outside* the
    # middleware's 1-second acquire budget. Every other test here passes just as well
    # over a cold pool on an idle machine, so name the contract explicitly.
    async def _run() -> int:
        async with warm_pool(migrated_url) as pool:
            return pool.get_stats()["pool_available"]

    assert asyncio.run(_run()) >= 1  # 0 if the pool were opened with wait=False


def test_recording_guard_ignores_unrelated_warnings() -> None:
    # Only the recording-failure warning may trip the guard; anything else this module
    # logs must not be misreported as a lost usage row.
    with recording_must_not_fail():
        logging.getLogger(UsageTrackingMiddleware.__module__).warning("slow write, not a failure")


def test_recording_guard_refuses_to_run_disarmed() -> None:
    # A raised `logging.disable` floor would leave the guard capturing nothing, so it
    # must fail rather than pass blindly — and must still restore the logger it touched.
    logger = logging.getLogger(UsageTrackingMiddleware.__module__)
    before = (logger.level, list(logger.handlers))
    logging.disable(logging.WARNING)
    try:
        with pytest.raises(AssertionError, match="disarmed"), recording_must_not_fail():
            pass  # pragma: no cover - the guard raises on entry
    finally:
        logging.disable(logging.NOTSET)
    assert (logger.level, list(logger.handlers)) == before
