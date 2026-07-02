"""`runs.install` and `runs.boot` MCP handlers."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.idempotency import delete_run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import InstallPayload, RunPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._idempotency import keyed_mutation
from kdive.mcp.tools.lifecycle.runs.common import run_job_envelope
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.steps import (
    existing_build_result,
    platform_owned_cmdline_token,
    step_progress,
)


async def install_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    cmdline: str | None = None,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit an idempotent install for a built, SUCCEEDED Run.

    ``cmdline`` (ADR-0299, #988) is an optional boot-cmdline override applied against the
    already-built kernel: it **replaces** any build-time extra args (platform tokens are always
    preserved). A value differing from the currently-installed one re-stages the boot without a
    rebuild; the same value is an idempotent no-op.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    owned = platform_owned_cmdline_token(cmdline)
    if owned is not None:
        return _config_error(
            run_id, data={"reason": "cmdline_overrides_platform_args", "token": owned}
        )
    if cmdline is not None and not cmdline.strip():
        return _config_error(run_id, data={"reason": "cmdline_blank"})
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.CONTRIBUTOR)
            if run.state is not RunState.SUCCEEDED:
                return _config_error(run_id, data={"current_status": run.state.value})
            if run.system_id is None:
                return _not_bound(run_id)
            return await keyed_mutation(
                conn,
                idempotency_key=idempotency_key,
                principal=ctx.principal,
                project=run.project,
                kind="runs.install",
                do_work=lambda: _restage_and_enqueue_install(conn, ctx, run, cmdline),
            )


async def _build_extra(conn: AsyncConnection, run: Run) -> str | None:
    """The build-baked cmdline extra on the ``build`` step (matches the install handler)."""
    result = await existing_build_result(conn, run.id)
    return result.cmdline if result is not None else None


async def _restage_and_enqueue_install(
    conn: AsyncConnection, ctx: RequestContext, run: Run, cmdline: str | None
) -> ToolResponse:
    """Enqueue install, re-staging when the requested cmdline differs from the installed one.

    The whole decision — read the step ledger, delete the settled ``install``/``boot`` rows on a
    re-stage, and enqueue — runs inside one per-Run advisory-lock transaction so a concurrent
    ``runs.install`` cannot interleave read→delete→enqueue (ADR-0299). ``_enqueue_step``'s
    ledger-driven recycle then carries the new cmdline into the recycled install job.
    """
    requested = cmdline.strip() if cmdline is not None else await _build_extra(conn, run)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        progress = await step_progress(conn, run.id)
        if progress.install == "running" or progress.boot == "running":
            return _config_error(str(run.id), data={"reason": "step_in_progress"})
        if progress.install == "succeeded" and progress.installed_cmdline != requested:
            await delete_run_step(conn, run.id, "install")
            await delete_run_step(conn, run.id, "boot")
        recycle = not await _has_step_row(conn, run.id, "install")
        job = await queue.enqueue(
            conn,
            JobKind.INSTALL,
            InstallPayload(run_id=str(run.id), cmdline=cmdline),
            job_authorizing(ctx, run.project),
            f"{run.id}:install",
            recycle_terminal=recycle,
        )
        # Include the cmdline in the audit args so a re-stage to a new cmdline is not audited
        # identically to the prior install (the args_digest is one-way, so this distinguishes the
        # operations without making the cmdline reverse-readable). Omitted when no override.
        audit_args: dict[str, str] = {"run_id": str(run.id)}
        if cmdline is not None:
            audit_args["cmdline"] = cmdline.strip()
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="runs.install",
                object_kind="runs",
                object_id=run.id,
                transition="install",
                args=audit_args,
                project=run.project,
            ),
        )
    return run_job_envelope(job, run.id)


async def boot_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit an idempotent boot for a built, installed Run."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.CONTRIBUTOR)
            if run.state is not RunState.SUCCEEDED:
                return _config_error(run_id, data={"current_status": run.state.value})
            if run.system_id is None:
                return _not_bound(run_id)
            if not await _has_succeeded_step(conn, uid, "install"):
                return _config_error(run_id, data={"reason": "install_first"})
            return await keyed_mutation(
                conn,
                idempotency_key=idempotency_key,
                principal=ctx.principal,
                project=run.project,
                kind="runs.boot",
                do_work=lambda: _enqueue_step(
                    conn,
                    ctx,
                    run,
                    JobKind.BOOT,
                    "boot",
                    "runs.boot",
                    payload=RunPayload(run_id=str(run.id)),
                ),
            )


def _not_bound(run_id: str) -> ToolResponse:
    """Reject install/boot of an unbound Run, pointing the agent at runs.bind (ADR-0169)."""
    return ToolResponse.failure(
        run_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail="run is not bound to a system",
        suggested_next_actions=["runs.bind"],
        data={"reason": "run_not_bound"},
    )


async def _has_succeeded_step(conn: AsyncConnection, run_id: UUID, step: str) -> bool:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT 1 FROM run_steps WHERE run_id = %s AND step = %s AND state = 'succeeded'",
            (run_id, step),
        )
        return await cur.fetchone() is not None


async def _has_step_row(conn: AsyncConnection, run_id: UUID, step: str) -> bool:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT 1 FROM run_steps WHERE run_id = %s AND step = %s", (run_id, step))
        return await cur.fetchone() is not None


async def _enqueue_step(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    kind: JobKind,
    step: str,
    tool: str,
    *,
    payload: RunPayload,
) -> ToolResponse:
    """Enqueue an install/boot step job under the per-Run lock.

    The recycle decision is ledger-driven (ADR-0299): a terminal job is recycled iff the step's
    ``run_steps`` row is absent. A present ``succeeded`` row is an idempotent no-op (the existing
    job is returned); a ``failed`` step's row was deleted by ``abandon_run_step`` so a retry
    recycles it; a re-stage (which deletes the row) likewise recycles, carrying the new payload.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        recycle = not await _has_step_row(conn, run.id, step)
        job = await queue.enqueue(
            conn,
            kind,
            payload,
            job_authorizing(ctx, run.project),
            f"{run.id}:{step}",
            recycle_terminal=recycle,
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool=tool,
                object_kind="runs",
                object_id=run.id,
                transition=step,
                args={"run_id": str(run.id)},
                project=run.project,
            ),
        )
    return run_job_envelope(job, run.id)
