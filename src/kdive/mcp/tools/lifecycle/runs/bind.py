"""MCP response adapter for `runs.bind` (ADR-0169)."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.responses import ToolResponse
from kdive.security.authz.context import RequestContext
from kdive.services.runs.bind import RunBindRequest as RunBindRequest
from kdive.services.runs.bind import RunBindResult
from kdive.services.runs.bind import bind_run as _bind_run
from kdive.services.runs.host_admission import RunCreateError


async def bind_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunBindRequest,
) -> ToolResponse:
    try:
        result = await _bind_run(pool, ctx, request)
    except RunCreateError as exc:
        return ToolResponse.failure_from_error(exc.object_id, exc)
    return _bound_response(result)


def _bound_response(result: RunBindResult) -> ToolResponse:
    return ToolResponse.success(
        str(result.run_id),
        "bound",
        suggested_next_actions=["runs.get", "runs.install"],
        data={"project": result.project, "system_id": str(result.system_id)},
    )


__all__ = ["RunBindRequest", "bind_run"]
