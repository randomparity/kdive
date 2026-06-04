"""The `runs.*` MCP tools — the Run join-point (ADR-0026).

`runs.create` binds a Run to a `ready` System (whose Allocation must be `active`, fixing
the Run's Allocation per the binding invariant) and an Investigation, and flips the
Investigation `open -> active` on its first Run — all in one transaction holding a
per-System then per-Investigation advisory lock (the global ALLOCATION→SYSTEM→
INVESTIGATION→RUN order). `runs.get` renders a Run; a `failed` Run maps to a failure
envelope carrying the Run's own `failure_category`. RBAC: `create` requires `operator`;
`get` requires project membership. Authz denials raise (ADR-0020: no authz ErrorCategory).
"""

from __future__ import annotations

from uuid import UUID

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RUNS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Run
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse

_RUN_HOSTABLE = frozenset({SystemState.READY})
_SYSTEM_GONE = frozenset({SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED})
_ALLOC_HOSTABLE = frozenset({AllocationState.ACTIVE})
_INVESTIGATION_OPEN_FOR_RUN = frozenset({InvestigationState.OPEN, InvestigationState.ACTIVE})


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _stale_handle(object_id: str, *, current_status: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.STALE_HANDLE, data={"current_status": current_status}
    )


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _envelope_for_run(run: Run) -> ToolResponse:
    """Render a Run; `failed` becomes a failure envelope carrying its `failure_category`."""
    if run.state is RunState.FAILED:
        category = run.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(str(run.id), category, data={"current_status": run.state.value})
    if run.state in (RunState.CREATED, RunState.RUNNING):
        actions = ["runs.get", "runs.build"]
    else:
        actions = ["runs.get"]
    return ToolResponse.success(
        str(run.id),
        run.state.value,
        suggested_next_actions=actions,
        data={"project": run.project},
    )


async def get_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Return a Run the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
        if run is None or run.project not in ctx.projects:
            return _config_error(run_id)
        return _envelope_for_run(run)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="runs.get")
    async def runs_get(run_id: str) -> ToolResponse:
        return await get_run(pool, current_context(), run_id)
