"""End-to-end proof that a real gateway call is recorded exactly once (#1625, #1640).

Pins ``usage.py``'s ``REENTRANT_TOOLS`` skip, since #1635 the denial boundary itself, and
since #1640 ``telemetry.py``'s skip as well. The file name predates the telemetry half and
is left alone because ADR-0486 cites the path; the module covers all three recording planes.

The telemetry half closes what the other two structurally could not. A telemetry regression
double-counts in spans and RED metrics rather than in a table, so before #1640 deleting
``telemetry.py:54``'s skip left every row assertion here green — the failure mode #1625 was
filed about, unguarded on the one plane that never touches Postgres. Reaching it needed a
seam: ``build_app`` hard-wired both handles to OTel's process-global providers, which are
set-once per process and shared across every test an ``-n auto --dist worksteal`` worker
runs. ADR-0487 made them injectable, so ``_recorders`` below scopes an in-memory span
exporter and metric reader to one app.

``tools.search`` is driven only on the telemetry plane, which is where #1654 changed it:
ADR-0485 splits the one skip set into ``REENTRANT_TOOLS`` for de-duplication and
``UNMETERED_TOOLS`` for volume, so ``tools.search`` stays out of ``tool_invocation`` while
gaining a span and RED metrics. Its absence from the usage rows here is a consequence of the
queries selecting the whole table, not an assertion aimed at it.

Epic #1576's criterion — *tool invocation and denial telemetry records the inner operation
once, not the gateway wrapper plus the inner call* — was covered by two halves that never
met. ``tests/mcp/middleware/test_gateway_skip.py`` asserts row counts against a real
migrated Postgres but hand-builds the middleware contexts and calls ``on_call_tool``
directly, so it never reaches ``gateway.py``'s ``app.call_tool(..., run_middleware=True)``
re-entry — the exact mechanism ``REENTRANT_TOOLS`` exists to de-duplicate.
``tests/mcp/tools/test_gateway_invoke.py`` drives that re-entry for real but against a pool
that is never opened, so it structurally cannot observe a row.

The gateway tests here join the halves: a real ``build_app`` over a real pool, driven through
``app.call_tool("tools.invoke", ...)``, asserting on the rows the middleware actually wrote.
Removing ``"tools.invoke"`` from ``REENTRANT_TOOLS`` reddens each of them (the outer chain then
records the gateway wrapper alongside the inner tool).

Both denial classes are now driven: the one a tool handler envelopes itself (non-member) and
the one that leaves the handler as an exception (a member's ``RoleDenied`` over-reach, the
class #1625 named and #1635 fixed). The second reaches the chain wrapped in ``ToolError``,
which is what made it unreachable before ADR-0486; its test asserts all three planes —
envelope, ``audit_log``, and ``tool_invocation.outcome``.

Scope — still narrower than #1625's full acceptance in one way worth stating plainly:
``UsageTrackingMiddleware``'s exception-carried exits are not driven here. Every case below
lands on its returned-result branch, because the denial boundary converts both denial classes
to a result before the usage recorder sees them. Its ``REENTRANT_TOOLS`` check is a single
early return ahead of the ``try``, so all three exits share one guard, but the raising paths
stay unpinned end to end.

Each gateway test carries a **positive control**: it calls the same inner tool directly as
well as through the gateway, and asserts two identical rows. Without it, "the outer chain ran
and ``REENTRANT_TOOLS`` suppressed its row" and "the outer chain never ran" are the same
observation — one absent row — and the redden proof above would evaporate silently if
``app.call_tool``'s ``run_middleware`` default ever changed. With it, a dead outer chain
yields one row and a stripped ``REENTRANT_TOOLS`` yields three. The telemetry tests carry the
same control on their own plane, in spans and metric points rather than rows. The two
transport tests are the exception: each drives one call through a real ``Client`` to pin the
transport shape of the denial — once as the SDK returns it, once as ``LiveStackClient``
parses it — where a second dispatch would add nothing. The harness one stands in for the
``live_stack`` tier, which ``just test`` excludes entirely and which would otherwise absorb
this change unobserved.

The identity half is real too: the token is minted, signed, and run through the same
``JWTVerifier`` the app is built with, so the claims the recorder attributes a row to are
the ones verification produced. The only production step skipped is the ASGI request-scope
lookup ``get_access_token`` prefers — with no HTTP request in-process it falls through to
``auth_context_var``, which is what these tests set. Everything from there down is
unpatched: ``current_context``, ``context_from_claims``, and the middleware-local
``middleware.shared.request_context`` the recorder reads through.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, LiteralString

from fastmcp import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.responses import ToolResponse
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair, mint
from tests.mcp.usage_support import (
    denial_audit_must_not_fail,
    recording_must_not_fail,
    warm_pool,
)

# A viewer on exactly one project: enough for session.whoami, and denied on any other
# project — the two outcomes these tests need from one token. Every claim the recorder
# writes to an asserted column is here, so none of them is decoration.
_PRINCIPAL = "viewer-user"
_AGENT_SESSION = "sess-viewer"
_CLIENT_ID = "test-client"
_PROJECT = "proj-a"


@dataclass(frozen=True, slots=True)
class _Recorders:
    """In-memory OTel collectors and the two handles ``build_app`` wires them in through."""

    span_exporter: InMemorySpanExporter
    metric_reader: InMemoryMetricReader
    tracer: Tracer
    meter: Meter

    def span_names(self) -> list[str]:
        """Every finished span name, in completion order."""
        return [span.name for span in self.span_exporter.get_finished_spans()]

    def metric_points(self) -> list[tuple[str, tuple[tuple[str, Any], ...], float]]:
        """Every recorded point as ``(metric, sorted labels, quantity)``, sorted.

        A counter point contributes its value and a histogram point its observation count —
        in both cases the quantity a double-count inflates. Sorted so the comparison is
        against the *set* of points, not against the order the SDK happens to collect them
        in, which is not part of any contract under test here.
        """
        data = self.metric_reader.get_metrics_data()
        points: list[tuple[str, tuple[tuple[str, Any], ...], float]] = []
        for resource_metric in data.resource_metrics if data is not None else ():
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    for point in metric.data.data_points:
                        quantity = (
                            point.value if isinstance(point, NumberDataPoint) else point.count
                        )
                        labels = tuple(sorted((point.attributes or {}).items()))
                        points.append((metric.name, labels, quantity))
        return sorted(points)


def _recorders() -> _Recorders:
    """Span and metric collectors scoped to a single ``build_app`` call.

    The providers are local objects, never installed with ``trace.set_tracer_provider`` /
    ``metrics.set_meter_provider``. Those are set-once per process and every test an
    ``-n auto --dist worksteal`` worker runs shares that process, so a global install would
    both no-op after the first test to try it and pool every other test's spans into this
    one's assertions. That set-once constraint is the whole reason ADR-0487 put the seam on
    ``build_app`` instead.

    The sampler is ``TracerProvider``'s default rather than the facade's
    ``ParentBased(TraceIdRatioBased(0.1))``, so a missing span below is a missing span
    rather than a sampling decision that would make these assertions flaky at 1-in-10.
    """
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    return _Recorders(
        span_exporter=span_exporter,
        metric_reader=metric_reader,
        tracer=tracer_provider.get_tracer("kdive.mcp"),
        meter=meter_provider.get_meter("kdive.mcp"),
    )


@contextlib.asynccontextmanager
async def _authenticated_app(
    pool: AsyncConnectionPool,
    *,
    recorders: _Recorders | None = None,
) -> AsyncIterator[Any]:
    """A real app, with a verified viewer token bound to the in-flight request context.

    The token is minted and then verified by the *same* ``JWTVerifier`` instance the app is
    built with, so the claims reaching the recorder are the ones token verification
    produced rather than a literal dict injected past it. That keeps a future fastmcp
    change to custom-claim handling — namespacing them, dropping unregistered ones,
    altering ``azp`` — a failure here rather than a silent loss of attribution in
    production.

    ``recorders`` routes ``TelemetryMiddleware``'s span and metric output to in-memory
    collectors through ADR-0487's ``build_app`` seam; left ``None``, the app reads the
    process-global providers exactly as production does.
    """
    keypair = make_keypair()
    verifier = JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(
        pool,
        verifier=verifier,
        secret_registry=SecretRegistry(),
        tracer=None if recorders is None else recorders.tracer,
        meter=None if recorders is None else recorders.meter,
    )
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


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict), f"expected structured_content dict, got {result!r}"
    return structured


async def _fetch(pool: AsyncConnectionPool, query: LiteralString) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(query)
        return await cur.fetchall()


# Attribution columns as well as the tool: the recorder resolves the caller through
# middleware.shared.request_context(), a different symbol from the one the inner tool
# reads, and it is the one the sibling tests monkeypatch. Selecting only (tool, outcome)
# would leave a row misattributed to a blank or stale principal indistinguishable from a
# correct one — which is the whole value of the row (ADR-0148).
#
# `project` is selected too, since #1644 taught `_call_project` to descend one level into a
# typed-request payload: `audit.query` nests its project under `request`, so the denial calls
# below now attribute their rows to a project rather than to NULL. Reverting that fix reddens
# both denial tests here — the attribution they already pinned is what the project rides on.
# On the `session.whoami` row the column stays NULL and is a control rather than a claim
# under test: that call carries no project at any depth, so no change to the resolver moves
# it. It is spelled out only to keep every row here compared as a whole tuple.
_USAGE_ROWS = (
    "SELECT tool, outcome, principal, agent_session, client_id, project "
    "FROM tool_invocation ORDER BY ts"
)


def test_real_gateway_call_records_one_usage_row_keyed_to_inner(migrated_url: str) -> None:
    """A real tools.invoke dispatch writes exactly one tool_invocation row: the inner tool."""

    async def _run() -> tuple[dict[str, Any], list[tuple[Any, ...]]]:
        async with warm_pool(migrated_url) as pool, _authenticated_app(pool) as app:
            with recording_must_not_fail():
                result = await app.call_tool(
                    "tools.invoke", {"name": "session.whoami", "arguments": {}}
                )
                # Positive control — the same tool, called directly. Its row is what proves
                # the outer chain runs at all, so the gateway's missing outer row is a
                # suppression rather than a chain that never executed.
                await app.call_tool("session.whoami", {})
            return _structured(result), await _fetch(pool, _USAGE_ROWS)

    envelope, rows = asyncio.run(_run())

    # The inner tool really ran — an inner failure would still write a row, with outcome
    # "error", so this pins the row below to a successful dispatch.
    assert envelope["status"] == "ok"
    assert envelope["data"]["principal"] == _PRINCIPAL

    # Two rows for two dispatches of the inner tool, each attributed to the verified token
    # and neither keyed to the wrapper. One row means the outer chain never ran; three
    # means "tools.invoke" left REENTRANT_TOOLS and the wrapper recorded itself.
    row = ("session.whoami", "ok", _PRINCIPAL, _AGENT_SESSION, _CLIENT_ID, None)
    assert rows == [row, row]


def test_real_gateway_denial_records_one_denied_usage_row_keyed_to_inner(
    migrated_url: str,
) -> None:
    """A denied tools.invoke dispatch writes exactly one denied usage row: the inner tool.

    Drives the denial class that reaches the middleware as a *result* rather than an
    exception. ``audit.query``'s project form gates on the caller's grants before it touches
    the pool, and its handler catches the non-member ``AuthorizationError`` and returns the
    ``authorization_denied`` envelope itself, so the chain sees an enveloped denial and
    ``UsageTrackingMiddleware`` classifies it from that envelope. That is the path
    ``REENTRANT_TOOLS`` has to de-duplicate on a denial outcome: without the skip the outer chain
    adds its own ``("tools.invoke", "denied")`` row.

    The exception-carried class — a member's ``RoleDenied`` over-reach — is the sibling test
    below. Nothing here asserts on ``audit_log``: no audit write is attempted on this path, so
    an assertion that none landed could not fail.
    """
    request = {"scope": "project", "project": "proj-not-granted"}

    async def _run() -> tuple[dict[str, Any], list[tuple[Any, ...]]]:
        async with warm_pool(migrated_url) as pool, _authenticated_app(pool) as app:
            with recording_must_not_fail():
                result = await app.call_tool(
                    "tools.invoke", {"name": "audit.query", "arguments": {"request": request}}
                )
                # Positive control — the same denial, called directly. See the success test.
                await app.call_tool("audit.query", {"request": request})
            return _structured(result), await _fetch(pool, _USAGE_ROWS)

    envelope, usage_rows = asyncio.run(_run())

    # The client-visible envelope is keyed to the inner tool, not to the gateway wrapper.
    # Independent of the rows below: object_id is chosen by the handler, where the row's
    # tool column comes from the MCP call name. Not asserting error_category here — the
    # middleware derives the row's "denied" outcome from it, so the rows already pin it.
    assert envelope["object_id"] == "audit.query"

    # Two rows for two dispatches of the inner tool. One means the outer chain never ran;
    # three means "tools.invoke" left REENTRANT_TOOLS and the wrapper recorded itself.
    row = ("audit.query", "denied", _PRINCIPAL, _AGENT_SESSION, _CLIENT_ID, "proj-not-granted")
    assert usage_rows == [row, row]


# The denial-audit row's own columns. `project` is asserted because it is the one field the
# boundary cannot recover from call arguments — it rides on `RoleDenied` itself — and
# `object_kind`/`object_id` because the boundary is object-agnostic by contract (ADR-0062 §5).
_AUDIT_ROWS = (
    "SELECT tool, principal, agent_session, project, transition, object_kind, object_id "
    "FROM audit_log ORDER BY ts"
)


def test_real_dispatch_role_denial_is_audited_enveloped_and_recorded_denied(
    migrated_url: str,
) -> None:
    """A member's role over-reach, driven through the real dispatch path (#1635, ADR-0486).

    The class the module docstring deferred. ``audit.query``'s project form requires ``admin``
    on the named project and re-raises ``RoleDenied`` rather than enveloping it locally, so
    with a ``viewer`` grant on that same project the denial leaves the handler as an exception
    and reaches the middleware chain the only way FastMCP delivers one: wrapped in
    ``ToolError``, with the denial demoted to ``__cause__``.

    Both the gateway and the direct dispatch are driven, and all three planes are asserted
    together — that is what makes this the pin for the whole boundary rather than for the
    envelope alone. Against pre-fix code every one of the three fails: the call raises
    ``ToolError`` out of ``call_tool``, ``audit_log`` stays empty, and the usage row records
    ``error``.
    """
    request = {"scope": "project", "project": _PROJECT}

    async def _run() -> tuple[dict[str, Any], dict[str, Any], list[Any], list[Any]]:
        async with warm_pool(migrated_url) as pool, _authenticated_app(pool) as app:
            with recording_must_not_fail(), denial_audit_must_not_fail():
                gateway = await app.call_tool(
                    "tools.invoke", {"name": "audit.query", "arguments": {"request": request}}
                )
                # Positive control — the same denial, called directly. See the success test.
                direct = await app.call_tool("audit.query", {"request": request})
            return (
                _structured(gateway),
                _structured(direct),
                await _fetch(pool, _AUDIT_ROWS),
                await _fetch(pool, _USAGE_ROWS),
            )

    gateway_envelope, direct_envelope, audit_rows, usage_rows = asyncio.run(_run())

    for envelope in (gateway_envelope, direct_envelope):
        # Keyed to the inner tool on both paths, never to the gateway wrapper.
        assert envelope["object_id"] == "audit.query"
        assert envelope["error_category"] == "authorization_denied"
        # ADR-0490's named role, reaching a client for the first time through this boundary:
        # `audit.query` never builds this envelope itself, so `admin` can only have come from
        # the `RoleDenied` the middleware unwrapped. Dropping `missing_roles` at the boundary
        # leaves the two assertions above green and only this one red.
        assert envelope["data"]["missing_roles"] == ["admin"]

    # `detail` stays ADR-0123's constant even though the role is named (ADR-0490): the grant
    # the caller lacks is disclosed, the resource behind it is not.
    assert gateway_envelope["detail"] == "access denied"

    audit_row = ("audit.query", _PRINCIPAL, _AGENT_SESSION, _PROJECT, "denied", None, None)
    assert audit_rows == [audit_row, audit_row]

    usage_row = ("audit.query", "denied", _PRINCIPAL, _AGENT_SESSION, _CLIENT_ID, _PROJECT)
    assert usage_rows == [usage_row, usage_row]


def test_role_denial_reaches_a_real_client_as_an_envelope(migrated_url: str) -> None:
    """The denial survives the transport, not just ``call_tool`` in process (ADR-0486).

    ``app.call_tool`` above returns the middleware's object as-is; the SDK ``tools/call``
    handler does not — ``mcp_operations._call_tool_mcp`` calls ``result.to_mcp_result()`` on
    it, a method only ``ToolResult`` has. A short-circuit returning a bare ``ToolResponse``
    therefore satisfies every assertion above and still reaches the client as an
    ``AttributeError`` dressed up as a ``ToolError``. That is the shape ``kdivectl`` and every
    MCP client see, and an enveloped result with a mapped ``error_category`` is what turns the
    denial into exit 3 rather than the generic exit 1 (``kdive.cli.errors``).

    **The call returning at all is the transport assertion.** ``Client.call_tool`` defaults to
    ``raise_on_error=True`` and raises before returning when ``isError`` is set
    (``fastmcp/client/mixins/tools.py:421``), so a bare ``ToolResponse`` — or any other result
    the transport cannot serialize — reddens this test by raising out of ``_run``. An explicit
    ``assert result.is_error is False`` would be unreachable, not redundant, so it is absent
    rather than merely omitted.
    """

    async def _run() -> Any:
        async with warm_pool(migrated_url) as pool, _authenticated_app(pool) as app:
            with recording_must_not_fail(), denial_audit_must_not_fail():
                async with Client(app) as client:
                    result = await client.call_tool(
                        "audit.query", {"request": {"scope": "project", "project": _PROJECT}}
                    )
            return result.structured_content

    structured = asyncio.run(_run())

    assert isinstance(structured, dict)
    assert structured["error_category"] == "authorization_denied"
    assert structured["data"]["missing_roles"] == ["admin"]


def test_role_denial_reaches_the_live_stack_harness_as_an_envelope(migrated_url: str) -> None:
    """``LiveStackClient`` parses the denial instead of raising ``LiveStackToolError``.

    The live-stack driver branches on the transport's ``is_error`` flag alone, so before
    ADR-0486 a `RoleDenied` reached it as a raise and its viewer negative asserted
    ``pytest.raises(LiveStackToolError)``. The denial is now an envelope, which flips that
    branch — and the whole live_stack tier is excluded from ``just test``, so nothing else in
    normal CI would notice.

    This is the deterministic stand-in for the wire test, joining the two halves that would
    otherwise never meet: that a real denial produces ``is_error=False`` (the test above) and
    that ``is_error=False`` takes the envelope-parsing branch
    (``tests/integration/live_stack/test_harness_tool_error.py``, over a fake client). Driving
    the real harness over a real denial is what makes the live-tier assertion predictable from
    a green CI run rather than from reading the code.
    """

    async def _run() -> ToolResponse | list[ToolResponse]:
        async with warm_pool(migrated_url) as pool, _authenticated_app(pool) as app:
            with recording_must_not_fail(), denial_audit_must_not_fail():
                harness = LiveStackClient(Client(app))
                async with harness:
                    return await harness.call_tool(
                        "audit.query", request={"scope": "project", "project": _PROJECT}
                    )

    denied = asyncio.run(_run())

    assert isinstance(denied, ToolResponse), "the harness raised instead of parsing the denial"
    assert denied.error_category == "authorization_denied"
    # The named role survives the harness's parse, which is what the live-tier viewer negative
    # now asserts over real HTTP for `allocations.request`'s `contributor`.
    assert denied.data["missing_roles"] == ["admin"]


# ============================================================
# The telemetry plane: spans and RED metrics through the real gateway (#1640)
# ============================================================


def test_real_gateway_call_emits_one_span_and_one_metric_set_per_inner_dispatch(
    migrated_url: str,
) -> None:
    """A real ``tools.invoke`` dispatch is traced and metered once: as the inner tool.

    The success arm of ``telemetry.py``'s ``REENTRANT_TOOLS`` skip, driven through
    ``build_app`` rather than a hand-built middleware. Deleting that skip reddens every
    assertion below: the outer chain then closes an ``mcp.tool/tools.invoke`` span around
    the inner one and adds its own ``requests``/``duration`` points beside it.

    Two dispatches of ``session.whoami`` — one through the gateway, one direct — are the
    positive control the sibling usage tests use for the same reason: with only the gateway
    call, "the outer chain ran and the skip suppressed its span" and "the outer chain never
    ran" are the same empty observation. The direct call's span and its second point on
    every instrument are what distinguish them.

    ``tools.search`` rides along because it is the *other* half of ADR-0485 §2 and §3
    recorded it as pinned at unit level only: it is not in ``REENTRANT_TOOLS``, so a real
    dispatch must be traced and metered like any other tool. Putting ``"tools.search"``
    back into the telemetry skip reddens this test and nothing else end to end.
    """
    recorders = _recorders()

    async def _run() -> dict[str, Any]:
        async with (
            warm_pool(migrated_url) as pool,
            _authenticated_app(pool, recorders=recorders) as app,
        ):
            with recording_must_not_fail():
                result = await app.call_tool(
                    "tools.invoke", {"name": "session.whoami", "arguments": {}}
                )
                # Positive control — the same tool, called directly. See the docstring.
                await app.call_tool("session.whoami", {})
                await app.call_tool("tools.search", {"query": "session.whoami", "limit": 1})
            return _structured(result)

    envelope = asyncio.run(_run())

    # The inner tool really ran: an inner failure would relabel every point below
    # outcome="error", so this pins the labels asserted next to a successful dispatch.
    assert envelope["status"] == "ok"

    assert recorders.span_names() == [
        "mcp.tool/session.whoami",  # the gateway dispatch's inner call
        "mcp.tool/session.whoami",  # the direct positive control
        "mcp.tool/tools.search",
    ]

    whoami = (("outcome", "ok"), ("tool", "session.whoami"))
    search = (("outcome", "ok"), ("tool", "tools.search"))
    # Compared as a whole list, so the absence of `kdive.mcp.request.errors` is asserted
    # too: an instrument with no measurement collects no data point, and a spurious error
    # add on this arm would appear here as a fourth entry.
    assert recorders.metric_points() == [
        ("kdive.mcp.request.duration", whoami, 2),
        ("kdive.mcp.request.duration", search, 1),
        ("kdive.mcp.requests", whoami, 2),
        ("kdive.mcp.requests", search, 1),
    ]


def test_real_gateway_denial_emits_one_error_span_and_metric_set_per_inner_dispatch(
    migrated_url: str,
) -> None:
    """The error arm of the same skip: a denied dispatch is counted once, as the inner tool.

    Telemetry classifies any enveloped ``error_category`` as ``ToolOutcome.ERROR``, so this
    drives the two instruments the success arm above leaves empty — the errors counter and
    the duration histogram *on the error path*. Both are asserted, because a regression that
    dropped only the error-arm histogram would leave a counter-only assertion green.

    The skip is one early return ahead of the span today, so this arm and the success arm
    share a guard; pinning them separately is what keeps a later narrowing of that guard to
    one exit from landing green.
    """
    recorders = _recorders()
    request = {"scope": "project", "project": "proj-not-granted"}

    async def _run() -> dict[str, Any]:
        async with (
            warm_pool(migrated_url) as pool,
            _authenticated_app(pool, recorders=recorders) as app,
        ):
            with recording_must_not_fail():
                result = await app.call_tool(
                    "tools.invoke", {"name": "audit.query", "arguments": {"request": request}}
                )
                # Positive control — the same denial, called directly. See the success test.
                await app.call_tool("audit.query", {"request": request})
            return _structured(result)

    envelope = asyncio.run(_run())

    # The denial the labels below are derived from: telemetry reads `error_category` off
    # this same envelope, so an inner call that succeeded would relabel every point.
    assert envelope["error_category"] == "authorization_denied"

    assert recorders.span_names() == ["mcp.tool/audit.query", "mcp.tool/audit.query"]

    denied = (("outcome", "error"), ("tool", "audit.query"))
    assert recorders.metric_points() == [
        ("kdive.mcp.request.duration", denied, 2),
        (
            "kdive.mcp.request.errors",
            (
                ("error_category", "authorization_denied"),
                ("outcome", "error"),
                ("tool", "audit.query"),
            ),
            2,
        ),
        ("kdive.mcp.requests", denied, 2),
    ]
