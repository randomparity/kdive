"""Read-side `runs.get` MCP handler."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS, RUNS, SYSTEMS
from kdive.domain.capacity.state import RunState
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools.lifecycle.runs.common import envelope_for_run
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.steps import install_method_for as _install_method_for
from kdive.services.runs.steps import system_required_cmdline


async def get_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Return a Run the caller's project owns, advertising the boot's required cmdline."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _not_found(run_id)
            require_role(ctx, run.project, Role.VIEWER)
            system = await SYSTEMS.get(conn, run.system_id) if run.system_id is not None else None
            runtime = await resolver.runtime_for_run(conn, run.id) if system is not None else None
            failing_job = (
                await JOBS.get(conn, run.failing_job_id)
                if run.state is RunState.FAILED and run.failing_job_id is not None
                else None
            )
        required = (
            system_required_cmdline(_install_method_for(system, runtime.profile_policy))
            if system is not None and runtime is not None
            else None
        )
        return envelope_for_run(run, required_cmdline=required, failing_job=failing_job)
