"""`runs.cancel` MCP handler."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import JOBS, RUNS
from kdive.domain.capacity.state import IllegalTransition, JobState, RunState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

_TERMINAL_JOB = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED})
_NEXT_ACTIONS = ["runs.create"]


async def cancel_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Drive a non-terminal Run to terminal ``canceled``, freeing its System (ADR-0158).

    Under the per-Run lock, transition a ``created``/``running`` Run to ``canceled`` and
    best-effort cancel its in-flight build job. A retried cancel on an already-``canceled``
    Run is an idempotent success no-op; a ``succeeded``/``failed`` Run returns ``conflict``
    (it is never relabeled). A bound Run's cancel frees the System for a new ``runs.create``
    with no ``systems.teardown``; an unbound Run (ADR-0169, ``system_id IS NULL``) has no System
    to free and cancel touches none, so it works the same way.

    Args:
        pool: The connection pool.
        ctx: The authenticated request context.
        run_id: The Run to cancel.

    Returns:
        A success envelope (``status="canceled"``) on cancel or idempotent no-op; a failure
        envelope (``not_found`` / ``configuration_error`` / ``conflict``) otherwise.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _not_found(run_id)
            require_role(ctx, run.project, Role.CONTRIBUTOR)
            return await _cancel_locked(conn, ctx, run)


async def _cancel_locked(conn: AsyncConnection, ctx: RequestContext, run: Run) -> ToolResponse:
    """Transition the Run + best-effort cancel its build job under the per-Run lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        locked = await RUNS.get(conn, run.id)
        prior = locked.state if locked is not None else run.state
        try:
            canceled = await RUNS.update_state(conn, run.id, RunState.CANCELED)
        except IllegalTransition:
            return await _terminal_response(conn, run)
        await _cancel_build_job_best_effort(conn, canceled.id)
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="runs.cancel",
                object_kind="runs",
                object_id=canceled.id,
                transition=f"{prior.value}->canceled",
                args={"run_id": str(canceled.id)},
                project=canceled.project,
            ),
        )
    return _canceled_response(canceled)


async def _terminal_response(conn: AsyncConnection, run: Run) -> ToolResponse:
    """Disambiguate an already-terminal Run after ``update_state`` raised ``IllegalTransition``.

    The re-read is fresh: ``update_state`` rolled back only its own inner savepoint, so the
    outer locked transaction is intact. An already-``canceled`` Run is an idempotent success;
    a ``succeeded``/``failed`` Run is a ``conflict`` naming the actual ``current_status``.
    """
    current = await RUNS.get(conn, run.id)
    state = current.state if current is not None else run.state
    if state is RunState.CANCELED:
        return _canceled_response(current or run)
    return ToolResponse.failure(
        str(run.id), ErrorCategory.CONFLICT, data={"current_status": state.value}
    )


async def _cancel_build_job_best_effort(conn: AsyncConnection, run_id: UUID) -> None:
    """Cancel the Run's in-flight build job if one is non-terminal; a no-op otherwise.

    Truly best-effort: the worker completes a build job via fenced raw SQL
    (``queue.complete``/``queue.fail``) that holds no per-Run lock, so a job read here as
    ``running`` can turn terminal before this ``FOR UPDATE`` acquires it. ``IllegalTransition``
    from that race is swallowed — a finished build job's result is moot once the Run is
    canceling, and the Run's own ``canceled`` transition must not roll back over it.
    """
    job = await queue.get_by_dedup_key(conn, f"{run_id}:build")
    if job is None or job.state in _TERMINAL_JOB:
        return
    try:
        await JOBS.update_state(conn, job.id, JobState.CANCELED)
    except IllegalTransition:
        return


def _canceled_response(run: Run) -> ToolResponse:
    return ToolResponse.success(
        str(run.id),
        "canceled",
        suggested_next_actions=_NEXT_ACTIONS,
        data={"project": run.project},
    )
