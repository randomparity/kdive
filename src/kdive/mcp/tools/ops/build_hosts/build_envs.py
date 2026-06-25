"""The build_envs.list discovery tool (ADR-0241): a contributor-readable projection of
build hosts as selectable build environments, omitting infra/secret detail."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.db.build_hosts import list_all_hosts
from kdive.mcp.responses import ToolResponse
from kdive.serialization import JsonValue

_TOOL = "build_envs.list"


async def list_build_envs(conn: AsyncConnection) -> ToolResponse:
    """Project registered build hosts into developer-facing build environments.

    Returns name, kind, the operator-asserted toolchain description, and enabled — never
    address, credential reference, or base-image volume (ADR-0241 §1).
    """
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
