"""`runs.build_install_boot` MCP handler (ADR-0268, #866).

Performs build-host selection and capacity admission (identical to ``runs.build``), then
enqueues a single :attr:`~kdive.domain.operations.jobs.JobKind.BUILD_INSTALL_BOOT` job
carrying :class:`~kdive.jobs.payloads.BuildInstallBootPayload`. The agent polls the one
job with ``jobs.wait``; the worker executes build → install → boot in sequence.

Requires :attr:`~kdive.security.authz.rbac.Role.OPERATOR` — the composite's highest-gate
phase (install/boot) runs at operator level (ADR-0268).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.components.references import CONFIG_COMPONENT, ComponentRef
from kdive.components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.domain.operations.jobs import BUILD_BEARING_JOB_KINDS, Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import BuildInstallBootPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._idempotency import keyed_mutation
from kdive.mcp.tools.lifecycle.runs.common import RUN_BUILD_TERMINAL, run_job_envelope
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.shared.build_host.configuration.config import config_refs
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.build_host_selection import resolve_and_admit
from kdive.services.runs.steps import platform_owned_cmdline_token

type ConfigValidator = Callable[[ComponentRef], None]


@dataclass(frozen=True, slots=True)
class CompositeRunHandlers:
    """Build→install→boot composite admission handler (ADR-0268, #866).

    Mirrors :class:`~kdive.mcp.tools.lifecycle.runs.server_build.BuildRunHandlers` —
    same admission path (build-host selection + capacity lease) — but enqueues a single
    :attr:`~kdive.domain.operations.jobs.JobKind.BUILD_INSTALL_BOOT` job instead of a
    bare :attr:`~kdive.domain.operations.jobs.JobKind.BUILD` job, and requires
    :attr:`~kdive.security.authz.rbac.Role.OPERATOR` (the max of its phases).

    Args:
        component_sources: Provider-declared config source capability set.
        config_validator: Optional extra check (provider root guard) run after the
            capability check; ``None`` skips it.
    """

    component_sources: ComponentSourceCapabilities
    config_validator: ConfigValidator | None = None

    async def build_install_boot(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        run_id: str,
        *,
        cmdline: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolResponse:
        """Admit and enqueue a composite build→install→boot job for a Run.

        Args:
            pool: The live connection pool (one connection is acquired).
            ctx: The caller's request context (principal, projects, roles).
            run_id: UUID string of the Run to drive.
            cmdline: Optional kernel debug args appended to the platform-required boot
                args (e.g. ``'dhash_entries=1'``). Bound at build time.
            idempotency_key: Optional replay-safe key; a repeated call returns the prior
                envelope without re-admitting or re-enqueuing.

        Returns:
            A ``queued`` job-handle envelope (same shape as ``runs.build``) on success,
            or a failure envelope on any admission error.
        """
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
                try:
                    for config_ref in config_refs(parsed):
                        reject_unsupported_component_source(
                            self.component_sources,
                            component_kind=CONFIG_COMPONENT,
                            ref=config_ref,
                        )
                        if self.config_validator is not None:
                            self.config_validator(config_ref)
                except CategorizedError as exc:
                    return ToolResponse.failure_from_error(run_id, exc)
                return await keyed_mutation(
                    conn,
                    idempotency_key=idempotency_key,
                    principal=ctx.principal,
                    project=run.project,
                    kind="runs.build_install_boot",
                    do_work=lambda: _composite_locked(conn, ctx, run, cmdline, parsed),
                )


async def _composite_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    cmdline: str | None,
    parsed_profile: ServerBuildProfile,
) -> ToolResponse:
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
            state = await _locked_run_state(conn, run)
            if state is not RunState.CREATED:
                existing = await queue.get_by_dedup_key(conn, _dedup_key(run))
                if existing is not None:
                    return run_job_envelope(existing, run.id)
            # Reject if a standalone runs.build (or a prior composite) is already live.
            # resolve_and_admit would surface this as a raw UniqueViolation on the
            # build_host_leases PK; catch it here as a typed configuration_error instead.
            if await _live_build_job_exists(conn, run.id):
                raise CategorizedError(
                    "a build job is already in progress for this run",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"reason": "build_already_in_progress"},
                )
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
                        tool="runs.build_install_boot",
                        object_kind="runs",
                        object_id=run.id,
                        transition="created->running",
                        args={"run_id": str(run.id)},
                        project=run.project,
                    ),
                )
            job = await _enqueue_composite(conn, ctx, run, cmdline, host_id=str(host.id))
    except CategorizedError as exc:
        next_actions = (
            ["runs.build_install_boot"]
            if exc.category is ErrorCategory.CAPACITY_EXHAUSTED
            else None
        )
        return ToolResponse.failure_from_error(
            str(run.id), exc, suggested_next_actions=next_actions
        )
    return run_job_envelope(job, run.id)


async def _live_build_job_exists(conn: AsyncConnection, run_id: UUID) -> bool:
    """Whether a queued/running build-bearing job already holds the slot for ``run_id``.

    Matches both ``build`` and ``build_install_boot``.  The composite's own dedup check
    above returns early when a prior ``build_install_boot`` job is live, so in practice
    this guard fires only when a standalone ``runs.build`` job is in progress.
    """
    cur = await conn.execute(
        "SELECT 1 FROM jobs WHERE kind = ANY(%s::text[]) "
        "AND (payload->>'run_id')::uuid = %s "
        "AND state IN ('queued', 'running') LIMIT 1",
        (list(BUILD_BEARING_JOB_KINDS), run_id),
    )
    return (await cur.fetchone()) is not None


async def _locked_run_state(conn: AsyncConnection, run: Run) -> RunState:
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


async def _enqueue_composite(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    cmdline: str | None,
    *,
    host_id: str,
) -> Job:
    payload = BuildInstallBootPayload(
        run_id=str(run.id),
        cmdline=cmdline if cmdline else None,
        build_host_id=host_id,
    )
    return await queue.enqueue(
        conn,
        JobKind.BUILD_INSTALL_BOOT,
        payload,
        job_authorizing(ctx, run.project),
        _dedup_key(run),
    )


def _dedup_key(run: Run) -> str:
    return f"{run.id}:build_install_boot"


__all__ = ["CompositeRunHandlers", "ConfigValidator"]
