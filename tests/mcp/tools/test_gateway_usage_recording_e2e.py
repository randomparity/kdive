"""End-to-end proof that a real gateway call writes one ``tool_invocation`` row (#1625).

Pins ``usage.py``'s ``META_TOOLS`` skip, and only that. The other two consumers #1625 names
stay unpinned end to end: ``telemetry.py``'s skip double-counts in metrics and spans rather
than in a table, so deleting it leaves both tests below green (#1640), and
``denial_audit.py``'s is unreachable for the separate reason under *Scope* (#1635).
``tools.search``, the other ``META_TOOLS`` *member*, is not driven through the real gateway
here either. A green run of this module is not an all-clear for the skip set as a whole.

Epic #1576's criterion — *tool invocation and denial telemetry records the inner operation
once, not the gateway wrapper plus the inner call* — was covered by two halves that never
met. ``tests/mcp/middleware/test_gateway_skip.py`` asserts row counts against a real
migrated Postgres but hand-builds the middleware contexts and calls ``on_call_tool``
directly, so it never reaches ``gateway.py``'s ``app.call_tool(..., run_middleware=True)``
re-entry — the exact mechanism ``META_TOOLS`` exists to de-duplicate.
``tests/mcp/tools/test_gateway_invoke.py`` drives that re-entry for real but against a pool
that is never opened, so it structurally cannot observe a row.

These two tests join the halves: a real ``build_app`` over a real pool, driven through
``app.call_tool("tools.invoke", ...)``, asserting on the rows the middleware actually wrote.
Removing ``"tools.invoke"`` from ``META_TOOLS`` reddens both (the outer chain then records
the gateway wrapper alongside the inner tool).

Scope — narrower than #1625's full acceptance, in two ways worth stating plainly:

* Only ``tool_invocation``. No ``audit_log`` assertion appears here at all.
* On the denial outcome, only the denial class a tool handler envelopes itself. The class
  #1625 named — a member's ``RoleDenied`` over-reach — never reaches the middleware chain
  as an exception on the real dispatch path: FastMCP wraps it in ``ToolError`` first, inside
  the branch the chain wraps. So it writes no ``audit_log`` row *and* records
  ``tool_invocation.outcome`` as ``error`` rather than ``denied``. **Both** halves of that
  class are deferred to #1635, which carries the fix and the assertions it unblocks.
* ``UsageTrackingMiddleware``'s exception-carried exits are not driven here — both cases
  below land on its returned-result branch. Its ``META_TOOLS`` check is a single early
  return ahead of the ``try``, so all three exits share one guard, but that path stays
  unpinned end to end. Noted on #1635, whose fix is what makes its ``outcome`` assertable.

Each test carries a **positive control**: it calls the same inner tool directly as well as
through the gateway, and asserts two identical rows. Without it, "the outer chain ran and
``META_TOOLS`` suppressed its row" and "the outer chain never ran" are the same observation —
one absent row — and the redden proof above would evaporate silently if
``app.call_tool``'s ``run_middleware`` default ever changed. With it, a dead outer chain
yields one row and a stripped ``META_TOOLS`` yields three.

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
from typing import Any, LiteralString

from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair, mint
from tests.mcp.usage_support import recording_must_not_fail, warm_pool

# A viewer on exactly one project: enough for session.whoami, and denied on any other
# project — the two outcomes these tests need from one token. Every claim the recorder
# writes to an asserted column is here, so none of them is decoration.
_PRINCIPAL = "viewer-user"
_AGENT_SESSION = "sess-viewer"
_CLIENT_ID = "test-client"
_PROJECT = "proj-a"


@contextlib.asynccontextmanager
async def _authenticated_app(pool: AsyncConnectionPool) -> AsyncIterator[Any]:
    """A real app, with a verified viewer token bound to the in-flight request context.

    The token is minted and then verified by the *same* ``JWTVerifier`` instance the app is
    built with, so the claims reaching the recorder are the ones token verification
    produced rather than a literal dict injected past it. That keeps a future fastmcp
    change to custom-claim handling — namespacing them, dropping unregistered ones,
    altering ``azp`` — a failure here rather than a silent loss of attribution in
    production.
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
# `project` is deliberately absent: it is NULL on the denial call below, because
# `_call_project` reads only top-level call arguments and `audit.query` nests its project
# under `request` (#1644). Selecting it would pin that gap rather than the attribution.
_USAGE_ROWS = (
    "SELECT tool, outcome, principal, agent_session, client_id FROM tool_invocation ORDER BY ts"
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
    # means "tools.invoke" left META_TOOLS and the wrapper recorded itself.
    row = ("session.whoami", "ok", _PRINCIPAL, _AGENT_SESSION, _CLIENT_ID)
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
    ``META_TOOLS`` has to de-duplicate on a denial outcome: without the skip the outer chain
    adds its own ``("tools.invoke", "denied")`` row.

    The exception-carried class — a member's ``RoleDenied`` over-reach, the one #1625 asked
    to mirror — is deferred to #1635 in both halves; see the module docstring. Nothing here
    asserts on ``audit_log``: no audit write is attempted on this path, so an assertion that
    none landed could not fail.
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
    # three means "tools.invoke" left META_TOOLS and the wrapper recorded itself.
    row = ("audit.query", "denied", _PRINCIPAL, _AGENT_SESSION, _CLIENT_ID)
    assert usage_rows == [row, row]
