"""MCP response adapter for `runs.create`."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.responses import ToolResponse
from kdive.security.authz.context import RequestContext
from kdive.services.runs.admission import RunCreateError, RunCreateResult
from kdive.services.runs.admission import RunCreateRequest as RunCreateRequest
from kdive.services.runs.admission import RunReuseRequirementInput as RunReuseRequirementInput
from kdive.services.runs.admission import create_run as _create_run


async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunCreateRequest,
) -> ToolResponse:
    try:
        result = await _create_run(pool, ctx, request)
    except RunCreateError as exc:
        return ToolResponse.failure_from_error(exc.object_id, exc)
    return _created_response(result)


def _created_response(result: RunCreateResult) -> ToolResponse:
    data = {
        "project": result.project,
        "investigation_id": str(result.investigation_id),
        "system_id": str(result.system_id),
    }
    if result.expected_boot_failure_kind is not None:
        data["expected_boot_failure"] = result.expected_boot_failure_kind
    return ToolResponse.success(
        str(result.run_id),
        "created",
        suggested_next_actions=["runs.get", "runs.build"],
        data=data,
    )


__all__ = ["RunCreateRequest", "RunReuseRequirementInput", "create_run"]
