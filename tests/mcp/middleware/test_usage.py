"""Cover the usage-tracking middleware: outcome classification + usage-row construction.

A pure unit module: every test here hand-builds a middleware context and calls the
middleware directly, so nothing in it needs Postgres. The end-to-end counterparts — the
project a *real* ``build_app`` dispatch carries reaching the row (#1644) — live in
``tests/mcp/tools/test_gateway_usage_recording_e2e.py``, where the container tier and the
authenticated-app helper already are. Keep new container-dependent tests there: parking
one here traded this module's sub-second runtime for a ~10s one.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware import usage as usage_mod
from kdive.mcp.middleware.shared import ToolOutcome
from kdive.mcp.middleware.usage import UsageTrackingMiddleware, _call_project
from kdive.mcp.responses import ToolResponse
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.usage import digest_args


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


# The nested forms (#1644). Six read/list tools take a typed request model, so the project
# reaches the transport one level down — under `request` for five of them and under `target`
# for accounting.usage. The resolver descends into any mapping-valued argument rather than
# matching those two names, so there is no key list to drift out of sync with the wrappers.


def test_call_project_descends_into_a_request_payload() -> None:
    arguments = {"request": {"scope": "project", "project": "proj-x"}}
    assert _call_project(_context(arguments=arguments)) == "proj-x"


def test_call_project_descends_into_a_target_payload() -> None:
    # accounting.usage's wrapper key, which is not `request` — a fix keyed on the literal
    # "request" leaves this tool broken and only this test red.
    arguments = {"target": {"kind": "project", "project": "proj-y"}}
    assert _call_project(_context(arguments=arguments)) == "proj-y"


def test_call_project_prefers_the_top_level_over_a_nested_one() -> None:
    arguments = {"project": "outer", "request": {"project": "inner"}}
    assert _call_project(_context(arguments=arguments)) == "outer"


def test_call_project_skips_a_payload_that_carries_no_project() -> None:
    # A mapping-valued argument without a `project` key is stepped over, not treated as an
    # answer: the scan continues to the next payload rather than stopping at the first
    # mapping it sees. Which payload wins when *two* carry a project is argument order, and
    # no wrapper has two model-typed params, so nothing here pins that.
    arguments = {"filters": {"state": "open"}, "request": {"project": "proj-z"}}
    assert _call_project(_context(arguments=arguments)) == "proj-z"


def test_call_project_none_for_non_mapping_payload() -> None:
    assert _call_project(_context(arguments={"request": "proj-x"})) is None


def test_call_project_none_for_nested_empty_string() -> None:
    assert _call_project(_context(arguments={"request": {"project": ""}})) is None


def test_call_project_none_for_nested_non_string() -> None:
    assert _call_project(_context(arguments={"request": {"project": 5}})) is None


def test_call_project_none_when_payload_has_no_project_key() -> None:
    assert _call_project(_context(arguments={"request": {"scope": "all"}})) is None


def test_call_project_does_not_descend_two_levels() -> None:
    # One level only. A deeper walk would attribute a row to whatever `project` key happens
    # to sit inside a nested sub-model, which no tool's project filter lives at.
    arguments = {"request": {"filters": {"project": "proj-deep"}}}
    assert _call_project(_context(arguments=arguments)) is None


def test_classify_ok_when_no_error_category() -> None:
    result = ToolResponse.success("runs.create", "created")
    assert UsageTrackingMiddleware._classify(result) is ToolOutcome.OK


def test_classify_denied_for_authorization_denied() -> None:
    result = ToolResponse.failure("runs.create", ErrorCategory.AUTHORIZATION_DENIED)
    assert UsageTrackingMiddleware._classify(result) is ToolOutcome.DENIED


def test_classify_error_for_other_category() -> None:
    result = ToolResponse.failure("runs.create", ErrorCategory.CONFIGURATION_ERROR)
    assert UsageTrackingMiddleware._classify(result) is ToolOutcome.ERROR


def _pool(value: object) -> AsyncConnectionPool:
    return cast("AsyncConnectionPool", value)


def _spy_middleware() -> tuple[UsageTrackingMiddleware, list[tuple[Any, ToolOutcome]]]:
    mw = UsageTrackingMiddleware(pool=_pool(object()), secret_registry=SecretRegistry())
    recorded: list[tuple[Any, ToolOutcome]] = []

    async def _record(ctx: Any, outcome: ToolOutcome) -> None:
        recorded.append((ctx, outcome))

    mw._record = _record  # ty: ignore[invalid-assignment]
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


# A `test_on_call_tool_records_denied_and_reraises_on_authorization_error` used to sit here. It
# raised `AuthorizationError` from `call_next` and asserted `DENIED` — the direct-`on_call_tool`
# shape ADR-0486 §4 disqualifies, pinning an arm no real dispatch could reach and that ADR-0493
# deleted. Not replaced: the envelope-classified denial it *looked* like it covered is already
# pinned by `test_classify_denied_for_authorization_denied` above (the classifier) and by
# `test_on_call_tool_records_classified_outcome_on_success` (that `on_call_tool` records what the
# classifier returns). The real-dispatch pin lives in
# `tests/mcp/tools/test_gateway_usage_recording_e2e.py`.


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
    mw = UsageTrackingMiddleware(
        pool=_pool(pool), secret_registry=SecretRegistry(), acquire_timeout=2.0
    )

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
    # args_digest is the redacted-args digest, populated on every recorded row (ADR-0304).
    assert isinstance(event.args_digest, str)
    assert len(event.args_digest) == 64
    expected = digest_args(Redactor(registry=SecretRegistry()), {"project": "demo"})
    assert event.args_digest == expected


def test_record_uses_question_mark_tool_when_message_has_no_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    _patch_record_boundary(monkeypatch, events, [])
    mw = UsageTrackingMiddleware(pool=_pool(_FakePool()), secret_registry=SecretRegistry())
    context = SimpleNamespace(message=SimpleNamespace())  # message lacks `name`

    asyncio.run(mw._record(context, ToolOutcome.OK))

    ((_conn, event),) = events
    assert event.tool == "?"


def test_record_default_acquire_timeout_is_one_second(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    _patch_record_boundary(monkeypatch, events, [])
    pool = _FakePool()
    mw = UsageTrackingMiddleware(
        pool=_pool(pool), secret_registry=SecretRegistry()
    )  # no acquire_timeout override

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
    mw = UsageTrackingMiddleware(pool=_pool(_FakePool()), secret_registry=SecretRegistry())

    # a failure inside _record must never propagate (best-effort recording)
    asyncio.run(mw._record(_context(name="runs.create"), ToolOutcome.OK))

    (args, kwargs) = warnings[0]
    assert args[0] == "usage recording failed for tool %s"
    assert args[1] == "runs.create"  # the tool name is logged
    assert kwargs["exc_info"] is True
