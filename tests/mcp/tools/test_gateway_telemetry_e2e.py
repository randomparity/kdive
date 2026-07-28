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

Scope: this covers ``tool_invocation``, on both the success and the denial outcome. The
matching ``audit_log`` proof is not here, because ``DenialAuditMiddleware`` does not see a
member's ``RoleDenied`` on the real dispatch path at all (#1635) — see the denial test.

Authentication goes through the SDK's own ``auth_context_var`` rather than a patched
``current_context``. That is the single seam the real HTTP path writes, so every reader
downstream — the inner tool, ``middleware.shared.request_context``, the denial audit —
resolves through the unpatched production accessor. Patching ``current_context`` instead
would need one patch per importing module, and each patch is a place the real lookup is no
longer under test.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any, LiteralString

from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair
from tests.mcp.usage_support import (
    denial_audit_must_not_fail,
    recording_must_not_fail,
    warm_pool,
)

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
    """Bind ``claims`` to the verified-token context var the real auth middleware sets."""
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
_DENIAL_ROWS = "SELECT tool FROM audit_log ORDER BY ts"


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


def test_real_gateway_denial_records_one_usage_row_keyed_to_inner(migrated_url: str) -> None:
    """A denied tools.invoke dispatch writes exactly one denied usage row: the inner tool.

    ``audit.query``'s project form gates on the caller's grants before it touches the pool,
    so naming a project the token does not carry denies at the authorization boundary — the
    same ``authorization_denied`` envelope a direct call produces, keyed to the inner tool.

    ``audit_log`` is asserted empty, not asserted to hold a row: a **non-member** denial is
    deliberately never audited (ADR-0043 §4 — openly-callable reads must not let an ordinary
    token amplify writes), and the audited class, a member's ``RoleDenied`` over-reach, does
    not reach ``DenialAuditMiddleware`` at all on the real dispatch path today (#1635).
    Asserting a denial row here would therefore be asserting a defect. The gateway half of
    that criterion lands with #1635's fix.
    """
    request = {"scope": "project", "project": "proj-not-granted"}

    async def _run() -> tuple[dict[str, Any], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        async with warm_pool(migrated_url) as pool:
            app = _build_app(pool)
            with (
                _authenticated(_VIEWER_CLAIMS),
                recording_must_not_fail(),
                denial_audit_must_not_fail(),
            ):
                result = await app.call_tool(
                    "tools.invoke", {"name": "audit.query", "arguments": {"request": request}}
                )
            return (
                _structured(result),
                await _fetch(pool, _USAGE_ROWS),
                await _fetch(pool, _DENIAL_ROWS),
            )

    envelope, usage_rows, denial_rows = asyncio.run(_run())

    # The call was denied at the authorization boundary, not rejected by argument binding
    # before it ever got there — an unreached gate would record outcome "error", not "denied".
    assert envelope["error_category"] == "authorization_denied"

    # One row, keyed to the inner tool. Without the META_TOOLS skip the outer chain also
    # records ("tools.invoke", "denied") and this is two rows.
    assert usage_rows == [("audit.query", "denied")]

    assert denial_rows == []
