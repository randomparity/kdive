"""Cover the usage-tracking middleware: outcome classification + usage-row construction."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, LiteralString, cast

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.assembly.app import build_app
from kdive.mcp.middleware import usage as usage_mod
from kdive.mcp.middleware.shared import ToolOutcome
from kdive.mcp.middleware.usage import UsageTrackingMiddleware, _call_project
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.rbac import AuthorizationError
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.usage import digest_args
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair, mint
from tests.mcp.usage_support import (
    denial_audit_must_not_fail,
    recording_must_not_fail,
    warm_pool,
)


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


def test_call_project_takes_the_first_payload_carrying_a_project() -> None:
    # Argument order decides, so the result is a function of the call rather than of dict
    # iteration luck: the earlier payload has no project, the later one does.
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


# --- end to end: the project a real call carried reaches the row (#1644) ---------------
#
# Everything above hand-builds a middleware context and calls the middleware directly, which
# is exactly the shape #1635 found stayed green through a real regression: 26 such tests
# passed while the denial boundary was broken end to end. The tests below drive `build_app`
# over a real migrated pool instead, so the argument mapping the resolver reads is the one
# FastMCP actually hands it for each wrapper shape, not one this module composed.

_PRINCIPAL = "viewer-user"
_AGENT_SESSION = "sess-viewer"
_CLIENT_ID = "test-client"
_PROJECT = "proj-a"

# Every tool that nests its project inside a typed request payload, with the outcome a
# viewer on `_PROJECT` gets from it. Both outcome classes are here on purpose: the issue's
# acceptance names `ok` and `denied` alike, and a denial is the case ADR-0148 keeps the row
# for — "who was denied on project X" is unanswerable while the column is NULL.
#
# The two denials are different denial classes, so neither stands in for the other:
# inventory.list envelopes its own platform-role refusal, while audit.query's project form
# re-raises `RoleDenied` and reaches the recorder through the boundary #1635 added.
_NESTED_PROJECT_CALLS = (
    ("investigations.list", {"request": {"project": _PROJECT}}, "ok"),
    ("allocations.list", {"request": {"project": _PROJECT}}, "ok"),
    ("debug.list_sessions", {"request": {"project": _PROJECT}}, "ok"),
    ("accounting.usage", {"target": {"kind": "project", "project": _PROJECT}}, "ok"),
    ("inventory.list", {"request": {"project": _PROJECT}}, "denied"),
    ("audit.query", {"request": {"scope": "project", "project": _PROJECT}}, "denied"),
)

_PROJECT_ROWS: LiteralString = "SELECT tool, outcome, project FROM tool_invocation ORDER BY ts"


@contextlib.asynccontextmanager
async def _viewer_app(pool: AsyncConnectionPool) -> AsyncIterator[Any]:
    """A real app with a verified viewer token on ``_PROJECT`` bound to the request context.

    The token is verified by the same ``JWTVerifier`` the app is built with, so the claims
    the recorder attributes a row to are the ones verification produced.
    """
    keypair = make_keypair()
    verifier = JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    token = await verifier.verify_token(
        mint(
            keypair,
            subject=_PRINCIPAL,
            agent_session=_AGENT_SESSION,
            projects=[_PROJECT],
            roles={_PROJECT: "viewer"},
            client_id=_CLIENT_ID,
        )
    )
    assert token is not None, "the app's own verifier rejected the minted viewer token"
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield app
    finally:
        auth_context_var.reset(reset)


async def _rows(pool: AsyncConnectionPool, query: LiteralString) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(query)
        return await cur.fetchall()


def test_nested_project_is_recorded_for_every_wrapper_payload_tool(migrated_url: str) -> None:
    """Each tool that nests ``project`` in a payload records it, on ``ok`` and ``denied``.

    Pre-fix every one of these six rows lands with ``project`` NULL, so the whole assertion
    is red. Reverting only the descent into non-``request`` keys leaves the accounting.usage
    row red alone, which is what makes the ``target`` wrapper covered rather than incidental.
    """

    async def _run() -> list[tuple[Any, ...]]:
        async with warm_pool(migrated_url) as pool, _viewer_app(pool) as app:
            with recording_must_not_fail(), denial_audit_must_not_fail():
                for name, arguments, _outcome in _NESTED_PROJECT_CALLS:
                    await app.call_tool(name, arguments)
            return await _rows(pool, _PROJECT_ROWS)

    rows = asyncio.run(_run())

    # The outcome column is asserted alongside the project so a call that stopped reaching
    # its handler — a payload the wrapper rejects, say — cannot pass as a recorded project.
    expected = [(name, outcome, _PROJECT) for name, _args, outcome in _NESTED_PROJECT_CALLS]
    assert rows == expected


def test_malformed_payload_records_a_null_project_rather_than_raising(
    migrated_url: str,
) -> None:
    """A non-mapping payload still lands a row, with ``project`` NULL.

    The resolver runs inside ADR-0148's best-effort recorder, so a payload it cannot walk
    has to yield ``None``, never an exception — a raise here would be swallowed and the row
    lost outright. Driven through the real app because only the app produces the argument
    mapping a rejected payload leaves behind: the wrapper's own validation fails the call,
    so the recorder sees the raw ``{"target": "proj-a"}`` on its error path.
    """

    async def _run() -> list[tuple[Any, ...]]:
        async with warm_pool(migrated_url) as pool, _viewer_app(pool) as app:
            with recording_must_not_fail(), contextlib.suppress(Exception):
                await app.call_tool("accounting.usage", {"target": _PROJECT})
            return await _rows(pool, _PROJECT_ROWS)

    rows = asyncio.run(_run())

    # One row, not zero: a raising resolver would be swallowed by the recorder and leave
    # none, so the row's presence is the totality claim and the NULL is the resolver's.
    assert rows == [("accounting.usage", "error", None)]
