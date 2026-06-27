"""The build_envs.list discovery tool (ADR-0242): a contributor-readable projection of
build hosts as selectable build environments, omitting infra/secret detail."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.db.build_hosts import list_all_hosts
from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, projects_with_role
from kdive.serialization import JsonValue

_TOOL = "build_envs.list"


async def list_build_envs(conn: AsyncConnection, ctx: RequestContext) -> ToolResponse:
    """Project registered build hosts into developer-facing build environments.

    Returns name, kind, the operator-asserted toolchain description, and enabled — never
    address, credential reference, or base-image volume (ADR-0242 §1).
    """
    if not projects_with_role(ctx, Role.CONTRIBUTOR):
        return ToolResponse.failure(
            _TOOL,
            ErrorCategory.AUTHORIZATION_DENIED,
            suggested_next_actions=[_TOOL],
        )
    hosts = await list_all_hosts(conn)
    envs: list[JsonValue] = [
        {
            "name": h.name,
            "kind": h.kind.value,
            "toolchain_desc": h.toolchain_desc,
            "enabled": h.enabled,
        }
        for h in hosts
    ]
    return ToolResponse.success(_TOOL, "ok", data={"build_envs": envs})
