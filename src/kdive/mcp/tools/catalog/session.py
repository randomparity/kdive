"""``session.whoami`` — a read-only identity / capability probe (ADR-0232, #752).

A pure projection of the request context: kdive has no DB identity table, so a caller's
identity and grants live entirely in its verified token (ADR-0006 / ADR-0043). This tool
reflects them back as one flat envelope so an agent can branch on its own claims —
"who am I, and what may I do?" — without a trial write against a mutating tool.

A plain authenticated read, with the same profile as ``projects.list`` (ADR-0117): it
requires a valid token (the verifier already gated the transport; ``current_context()``
enforces presence as defence in depth), but there is no platform gate, no project gate,
and no audit — the response is the caller's own token claims, with no cross-tenant data.
It is registered in ``PUBLIC_TOOLS`` so viewer and role-less callers are admitted; gating
the probe behind a role would defeat its purpose. ``agent_session`` is deliberately not
returned (it is a session-correlation token for audit, not an identity claim).
"""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.security.authz.context import RequestContext


def whoami(ctx: RequestContext) -> ToolResponse:
    """Project ``ctx`` into the flat identity envelope (ADR-0232).

    Returns a single success envelope (``object_id`` is the principal) whose ``data``
    carries ``principal``, ``client_id`` (``None`` when the token has no ``azp``/
    ``client_id``), the sorted de-duplicated ``projects`` list (``ctx.projects`` is not
    de-duplicated upstream), the ``roles`` map (role-bearing projects only — a role-less
    membership shows in ``projects`` but not ``roles``), and the sorted ``platform_roles``
    list. Every key is always present, so a caller reads them unconditionally.
    """
    projects: list[JsonValue] = list(sorted(set(ctx.projects)))
    roles: dict[str, JsonValue] = {
        project: role.value for project, role in sorted(ctx.roles.items())
    }
    platform_roles: list[JsonValue] = list(sorted(role.value for role in ctx.platform_roles))
    return ToolResponse.success(
        ctx.principal,
        "ok",
        suggested_next_actions=["projects.list"],
        data={
            "principal": ctx.principal,
            "client_id": ctx.client_id,
            "projects": projects,
            "roles": roles,
            "platform_roles": platform_roles,
        },
    )


def register(app: FastMCP, _pool: AsyncConnectionPool) -> None:
    """Register ``session.whoami`` on ``app`` (a pure context projection; no pool use)."""

    @app.tool(
        name="session.whoami",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def session_whoami() -> ToolResponse:
        """Return the caller's own identity: principal, client, projects, roles, platform roles."""
        return whoami(current_context())
