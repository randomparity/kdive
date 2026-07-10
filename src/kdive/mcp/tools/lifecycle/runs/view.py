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
from kdive.mcp.tools.debug.sessions_read import active_session_ids_for_run
from kdive.mcp.tools.lifecycle.runs.common import envelope_for_run
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.artifacts.listing import list_run_console_artifacts
from kdive.services.runs.steps import existing_build_result as _existing_build_result
from kdive.services.runs.steps import failed_boot_attempt as _failed_boot_attempt
from kdive.services.runs.steps import install_method_for as _install_method_for
from kdive.services.runs.steps import step_progress as _step_progress
from kdive.services.runs.steps import system_arch as _system_arch
from kdive.services.runs.steps import system_required_cmdline


async def get_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    resolver: ProviderResolver,
    include_console_artifacts: bool = False,
) -> ToolResponse:
    """Return a Run the caller's project owns, advertising the boot's required cmdline.

    The Run-scoped console manifest (`data.console_artifacts`, ADR-0279) is opt-in: it is fetched
    and inlined only when ``include_console_artifacts`` is true (#1067, ADR-0324). By default the
    manifest is neither queried nor rendered — the boot snapshot stays at ``refs.console``.
    """
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
            active_sessions = await active_session_ids_for_run(conn, run.id)
            progress = (
                await _step_progress(conn, run.id) if run.state is RunState.SUCCEEDED else None
            )
            build_result = (
                await _existing_build_result(conn, run.id)
                if run.state is RunState.SUCCEEDED
                else None
            )
            boot_attempt = (
                await _failed_boot_attempt(conn, run.id)
                if progress is not None and progress.boot != "succeeded"
                else None
            )
            # The Run-scoped console manifest (ADR-0279), opt-in per #1067/ADR-0324. Queried inside
            # the open connection (it closes before envelope_for_run) only when the caller asked;
            # always skipped for a failed Run, whose envelope omits it.
            console_manifest = (
                await list_run_console_artifacts(conn, run.id)
                if include_console_artifacts and run.state is not RunState.FAILED
                else None
            )
        required = (
            system_required_cmdline(
                _install_method_for(system, runtime.profile_policy),
                runtime.platform_root_cmdline,
                arch=_system_arch(system),
            )
            if system is not None and runtime is not None
            else None
        )
        return envelope_for_run(
            run,
            required_cmdline=required,
            failing_job=failing_job,
            active_debug_session_ids=active_sessions,
            step_progress=progress,
            boot_readiness=boot_attempt,
            build_provenance=build_result.build_provenance if build_result is not None else None,
            console_manifest=console_manifest,
        )
