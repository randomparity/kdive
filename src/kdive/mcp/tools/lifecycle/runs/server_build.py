"""`runs.build` MCP handler."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.build_configs.defaults import DEFAULT_CONFIG_REF
from kdive.components.references import CONFIG_COMPONENT, ComponentRef
from kdive.components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import Run
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import BuildPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.runs.common import RUN_BUILD_TERMINAL, run_job_envelope
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.build_host_selection import resolve_and_admit
from kdive.services.runs.steps import platform_owned_cmdline_token

type ConfigValidator = Callable[[ComponentRef], None]


@dataclass(frozen=True, slots=True)
class BuildRunHandlers:
    """Server-build admission handler with provider validation seams."""

    component_sources: ComponentSourceCapabilities
    config_validator: ConfigValidator | None = None

    async def build_run(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        run_id: str,
        *,
        cmdline: str | None = None,
    ) -> ToolResponse:
        """Admit an idempotent server build for a Run and enqueue the build job."""
        uid = _as_uuid(run_id)
        if uid is None:
            return _config_error(run_id)
        owned = platform_owned_cmdline_token(cmdline)
        if owned is not None:
            return _config_error(
                run_id, data={"reason": "cmdline_overrides_platform_args", "token": owned}
            )
        with bind_context(principal=ctx.principal):
            async with pool.connection() as conn:
                run = await RUNS.get(conn, uid)
                if run is None or run.project not in ctx.projects:
                    return _config_error(run_id)
                require_role(ctx, run.project, Role.OPERATOR)
                try:
                    parsed = BuildProfile.parse(run.build_profile)
                except CategorizedError as exc:
                    return ToolResponse.failure_from_error(run_id, exc)
                if not isinstance(parsed, ServerBuildProfile):
                    return _config_error(
                        run_id, data={"reason": "external_source_uses_complete_build"}
                    )
                # An omitted config validates against the kdump catalog default, matching the
                # resolver substitution on the build path (ADR-0096).
                config_ref = parsed.config or DEFAULT_CONFIG_REF
                try:
                    reject_unsupported_component_source(
                        self.component_sources,
                        component_kind=CONFIG_COMPONENT,
                        ref=config_ref,
                    )
                except CategorizedError as exc:
                    return ToolResponse.failure_from_error(run_id, exc)
                if self.config_validator is not None:
                    try:
                        self.config_validator(config_ref)
                    except CategorizedError as exc:
                        return ToolResponse.failure_from_error(run_id, exc)
                return await _build_locked(conn, ctx, run, cmdline, parsed)


async def _build_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    cmdline: str | None,
    parsed_profile: ServerBuildProfile,
) -> ToolResponse:
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
            state = await _locked_build_state(conn, run)
            if state is not RunState.CREATED:
                existing = await queue.get_by_dedup_key(conn, _build_dedup_key(run))
                if existing is not None:
                    return run_job_envelope(existing, run.id)
            host = await resolve_and_admit(conn, parsed_profile, run.id)
            if state is RunState.CREATED:
                await conn.execute(
                    "UPDATE runs SET state = %s WHERE id = %s AND state = %s",
                    (RunState.RUNNING.value, run.id, RunState.CREATED.value),
                )
                await audit.record(
                    conn,
                    ctx,
                    audit.AuditEvent(
                        tool="runs.build",
                        object_kind="runs",
                        object_id=run.id,
                        transition="created->running",
                        args={"run_id": str(run.id)},
                        project=run.project,
                    ),
                )
            job = await _enqueue_build(conn, ctx, run, cmdline, host_id=str(host.id))
    except CategorizedError as exc:
        next_actions = ["runs.build"] if exc.category is ErrorCategory.CAPACITY_EXHAUSTED else None
        return ToolResponse.failure_from_error(
            str(run.id), exc, suggested_next_actions=next_actions
        )
    return run_job_envelope(job, run.id)


async def _locked_build_state(conn: AsyncConnection, run: Run) -> RunState:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError(str(run.id), category=ErrorCategory.CONFIGURATION_ERROR)
    state = RunState(row["state"])
    if state in RUN_BUILD_TERMINAL:
        raise CategorizedError(
            str(run.id),
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"current_status": state.value},
        )
    return state


async def _enqueue_build(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    cmdline: str | None,
    *,
    host_id: str,
) -> Job:
    payload = BuildPayload(
        run_id=str(run.id),
        cmdline=cmdline if cmdline else None,
        build_host_id=host_id,
    )
    return await queue.enqueue(
        conn,
        JobKind.BUILD,
        payload,
        job_authorizing(ctx, run.project),
        _build_dedup_key(run),
    )


def _build_dedup_key(run: Run) -> str:
    return f"{run.id}:build"


__all__ = ["BuildRunHandlers", "ConfigValidator"]
