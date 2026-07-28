"""End-to-end proof that one real gateway call records telemetry exactly once (#1625).

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

``tools.search`` and the telemetry recorder — the other two ``META_TOOLS`` consumers #1625
names — remain covered only by the direct-call tests in ``test_gateway_skip.py``.

Authentication binds the SDK's ``auth_context_var`` rather than patching ``current_context``.
That is not quite the production write: ``fastmcp.server.dependencies.get_access_token``
reads the HTTP request scope first and falls back to that context var, so an in-process call
with no HTTP request exercises the fallback. It is still the closest reachable seam — every
reader downstream (the inner tool, ``middleware.shared.request_context``, the denial audit)
resolves through the unpatched production accessor and the real ``context_from_claims``.
Patching ``current_context`` instead needs one patch per importing module, and each patch is
a place the real lookup stops being under test.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any, LiteralString

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair
from tests.mcp.usage_support import recording_must_not_fail, warm_pool

# A viewer on exactly one project: enough for session.whoami, and denied on any other
# project — the two outcomes these tests need from one token.
_VIEWER_CLAIMS: dict[str, Any] = {
    "sub": "viewer-user",
    "agent_session": "sess-viewer",
    "projects": ["proj-a"],
    "roles": {"proj-a": "viewer"},
}


@contextlib.contextmanager
def _authenticated(claims: dict[str, Any]) -> Iterator[None]:
    """Bind ``claims`` to the verified-token context var the auth middleware sets.

    FastMCP's own ``AccessToken``, not the SDK base class: it is what ``JWTVerifier``
    produces in production, and ``get_access_token`` returns it from an ``isinstance``
    fast path rather than rebuilding it through the ``model_dump`` conversion shim it
    keeps for foreign token types.
    """
    token = AccessToken(token="test-token", client_id="test-client", scopes=[], claims=claims)
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def _build_app(pool: AsyncConnectionPool) -> Any:
    keypair = make_keypair()
    verifier = JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)
    return build_app(pool, verifier=verifier, secret_registry=SecretRegistry())


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict), f"expected structured_content dict, got {result!r}"
    return structured


async def _fetch(pool: AsyncConnectionPool, query: LiteralString) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(query)
        return await cur.fetchall()


_USAGE_ROWS = "SELECT tool, outcome FROM tool_invocation ORDER BY ts"


def test_real_gateway_call_records_one_usage_row_keyed_to_inner(migrated_url: str) -> None:
    """A real tools.invoke dispatch writes exactly one tool_invocation row: the inner tool."""

    async def _run() -> tuple[dict[str, Any], list[tuple[Any, ...]]]:
        async with warm_pool(migrated_url) as pool:
            app = _build_app(pool)
            with _authenticated(_VIEWER_CLAIMS), recording_must_not_fail():
                result = await app.call_tool(
                    "tools.invoke", {"name": "session.whoami", "arguments": {}}
                )
            return _structured(result), await _fetch(pool, _USAGE_ROWS)

    envelope, rows = asyncio.run(_run())

    # The inner tool really ran — an inner failure would still write a row, with outcome
    # "error", so this pins the row below to a successful dispatch.
    assert envelope["status"] == "ok"
    assert envelope["data"]["principal"] == "viewer-user"

    # One row, keyed to the inner tool. Without the META_TOOLS skip the outer chain also
    # records "tools.invoke" and this is two rows.
    assert rows == [("session.whoami", "ok")]


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
        async with warm_pool(migrated_url) as pool:
            app = _build_app(pool)
            with _authenticated(_VIEWER_CLAIMS), recording_must_not_fail():
                result = await app.call_tool(
                    "tools.invoke", {"name": "audit.query", "arguments": {"request": request}}
                )
            return _structured(result), await _fetch(pool, _USAGE_ROWS)

    envelope, usage_rows = asyncio.run(_run())

    # The call was denied on its grants, not rejected by argument binding before the gate
    # was ever reached — an unreached gate would record outcome "error", not "denied".
    assert envelope["error_category"] == "authorization_denied"

    # One row, keyed to the inner tool. Without the META_TOOLS skip the outer chain also
    # records ("tools.invoke", "denied") and this is two rows.
    assert usage_rows == [("audit.query", "denied")]
