"""``projects.list`` — whoami discovery for the caller's token-derived grants (ADR-0117).

A pure projection of the request context: kdive has no DB project table, so a caller's
grants live entirely in its verified token (``roles_from_claims`` /
``platform_roles_from_claims``). This tool reflects them back so an agent can discover
"what may I touch?" without guessing project names by trial.

A plain authenticated read: it requires a valid token (the verifier already gated the
transport; ``current_context()`` enforces presence as defence in depth), but there is no
platform gate, no project gate, and no audit — the response is the caller's own token
claims, with no cross-tenant data. Each granted project flattens to ``{project, role}``
(``role`` is ``""`` for a role-less membership — the honest "member but no role" signal);
the top-level ``data`` always carries ``principal`` and a (possibly empty)
``platform_roles`` list.
"""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.security.authz.context import RequestContext

_OBJECT_ID = "projects"


def whoami(ctx: RequestContext) -> ToolResponse:
    """Project ``ctx`` into the granted-projects whoami envelope (ADR-0117).

    Returns one item per granted project (``{project, role}``, ``role=""`` when the
    membership carries no role), sorted by project name and deduplicated (``ctx.projects``
    is not deduplicated upstream). The top-level ``data`` carries the token ``principal``
    and the sorted ``platform_roles`` (a list, empty for a project-only token).
    """
    items: list[ToolResponse] = []
    for project in sorted(set(ctx.projects)):
        role = ctx.roles.get(project)
        items.append(
            ToolResponse.success(
                project,
                "ok",
                data={"project": project, "role": role.value if role is not None else ""},
            )
        )
    platform_roles: list[JsonValue] = list(sorted(role.value for role in ctx.platform_roles))
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=["systems.list", "runs.list", "accounting.report_granted_set"],
        data={"principal": ctx.principal, "platform_roles": platform_roles},
    )


def register(app: FastMCP, _pool: AsyncConnectionPool) -> None:
    """Register ``projects.list`` on ``app`` (a pure context projection; no pool use)."""

    @app.tool(
        name="projects.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def projects_list() -> ToolResponse:
        """List the caller's granted projects, their roles, and the caller's platform roles."""
        return whoami(current_context())
