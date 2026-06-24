"""`runs.install` and `runs.boot` MCP handlers."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle import Run
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import RunPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.runs.common import run_job_envelope
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.idempotency.envelope import keyed_mutation


async def install_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit an idempotent install for a built, SUCCEEDED Run."""
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
            return await keyed_mutation(
                conn,
                idempotency_key=idempotency_key,
                principal=ctx.principal,
                project=run.project,
                kind="runs.install",
                do_work=lambda: _enqueue_step(
                    conn, ctx, run, JobKind.INSTALL, "install", "runs.install"
                ),
            )


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
                do_work=lambda: _enqueue_step(conn, ctx, run, JobKind.BOOT, "boot", "runs.boot"),
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


async def _enqueue_step(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    kind: JobKind,
    step: str,
    tool: str,
) -> ToolResponse:
    """Enqueue an install/boot step job under the per-Run lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        job = await queue.enqueue(
            conn,
            kind,
            RunPayload(run_id=str(run.id)),
            job_authorizing(ctx, run.project),
            f"{run.id}:{step}",
            # A terminally-failed step is recycled to a fresh attempt so a transient blip can be
            # retried in place without a rebuild; the per-Run lock serializes retries (ADR-0185).
            retry_terminal_failed=True,
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
