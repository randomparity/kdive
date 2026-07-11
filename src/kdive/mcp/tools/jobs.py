"""The `jobs.*` MCP tools over the durable queue.

Each tool is a thin FastMCP wrapper over a plain async handler that takes its
dependencies (the pool, the request context) as arguments, so handlers are tested
directly without MCP transport. A handler that raises a domain error becomes an
error :class:`~kdive.mcp.responses.ToolResponse` (with the most specific
``ErrorCategory``), never an unhandled 500.

Every read/cancel is **project-scoped**: a job is visible only to a caller with
``viewer`` on the owning project (``authorizing->>'project'``). Cancellation requires
``contributor`` for leaseholder-lifecycle kinds — including the provision lane
(provision/reprovision) — matching ``runs.cancel``, and ``operator`` for the destructive kinds
(teardown/force_crash), keyed off the job's kind, not its enqueuing principal. A by-id read or
cancel of a job in an ungranted
project returns the same
not-found-shaped error as a missing job, so existence is not leaked (matching
``systems``/``runs``/``allocations`` getters); ``list`` returns only readable jobs.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import JOBS, ObjectNotFound
from kdive.domain.capacity.state import IllegalTransition, JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import CONTRIBUTOR_CANCELABLE_JOB_KINDS, Job, JobKind
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, InvalidCursor
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools._common import paginate as _paginate
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, Role, RoleDenied, require_role

_log = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.5
DEFAULT_WAIT_S = 30.0
MAX_WAIT_S = 300.0

_TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED})


class _JobsListPayload(ToolPayload):
    """Public payload for ``jobs.list`` filters and pagination."""

    status: JobState | None = Field(default=None, description="Only jobs in this lifecycle state.")
    kind: JobKind | None = Field(default=None, description="Only jobs of this kind.")
    investigation_id: str | None = Field(
        default=None,
        description=(
            "Only run-bearing jobs (build/install/boot) whose Run belongs to this Investigation."
        ),
    )
    limit: int = Field(
        default=DEFAULT_LIST_LIMIT,
        description=f"Maximum rows returned (capped at {MAX_LIST_LIMIT}).",
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
    )


def _error(object_id: str, category: ErrorCategory) -> ToolResponse:
    return ToolResponse.failure(object_id, category)


def _job_response(job: Job) -> ToolResponse:
    try:
        return ToolResponse.from_job(job)
    except ValueError:
        _log.warning(
            "job %s violates the response invariant; degraded",
            job.id,
            exc_info=True,
        )
        return _error(str(job.id), ErrorCategory.INFRASTRUCTURE_FAILURE)


def _in_scope(job: Job, ctx: RequestContext) -> bool:
    """True iff ``job``'s owning project is granted to ``ctx``."""
    return job.authorizing["project"] in ctx.projects


def _project(job: Job) -> str:
    project = job.authorizing["project"]
    return str(project)


def _readable_projects(ctx: RequestContext) -> list[str]:
    readable: list[str] = []
    for project in ctx.projects:
        try:
            require_role(ctx, project, Role.VIEWER)
        except AuthorizationError:
            continue
        readable.append(project)
    return readable


def _cancel_role(kind: JobKind) -> Role:
    """The role required to cancel a job of ``kind``, keyed off kind not enqueuing principal.

    A contributor may cancel a leaseholder-lifecycle job it can itself start
    (``CONTRIBUTOR_CANCELABLE_JOB_KINDS``); every other kind keeps the fail-closed operator gate.
    """
    return Role.CONTRIBUTOR if kind in CONTRIBUTOR_CANCELABLE_JOB_KINDS else Role.OPERATOR


def _require_job_role(
    job: Job,
    ctx: RequestContext,
    role: Role,
    object_id: str,
) -> ToolResponse | None:
    try:
        require_role(ctx, _project(job), role)
    except RoleDenied:
        raise
    except AuthorizationError:
        return _error(object_id, ErrorCategory.AUTHORIZATION_DENIED)
    return None


async def get_job(pool: AsyncConnectionPool, ctx: RequestContext, job_id: str) -> ToolResponse:
    """Return the job's handle envelope, or an error envelope if absent/malformed.

    Binds the request's ``principal`` + ``job_id`` into the structured-log context
    (ADR-0014) so every record emitted while serving this read is attributed, whether
    the handler is reached through the MCP tool or called directly.
    """
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    with bind_context(principal=ctx.principal, job_id=job_id):
        async with pool.connection() as conn:
            job = await JOBS.get(conn, uid)
        if job is None or not _in_scope(job, ctx):
            return _not_found(job_id)
        denied = _require_job_role(job, ctx, Role.VIEWER, job_id)
        if denied is not None:
            return denied
        return _job_response(job)


async def wait_job(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    job_id: str,
    timeout_s: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ToolResponse:
    """Poll until the job is terminal or ``timeout_s`` (clamped to ``MAX_WAIT_S``) elapses.

    Each poll acquires and releases a pool connection (holds none while sleeping). A
    non-positive timeout means a single read. On a non-terminal timeout this returns the
    job's current (``queued``/``running``) envelope with ``jobs.wait`` in
    ``suggested_next_actions`` — the bounded "still running, call again" signal.

    The agent-facing retry contract (short waits over one long hold; a transport drop on a
    long wait is transient and retryable) lives on the ``jobs_wait`` wrapper docstring, which
    is the text serialized into the tool schema; keep the two in step. See ADR-0138.
    """
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    if not math.isfinite(timeout_s):
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(max(timeout_s, 0.0), MAX_WAIT_S)
    with bind_context(principal=ctx.principal, job_id=job_id):
        while True:
            async with pool.connection() as conn:
                job = await JOBS.get(conn, uid)
            if job is None or not _in_scope(job, ctx):
                return _not_found(job_id)
            denied = _require_job_role(job, ctx, Role.VIEWER, job_id)
            if denied is not None:
                return denied
            now = loop.time()
            if job.state in _TERMINAL or now >= deadline:
                return _job_response(job)
            await sleep(min(POLL_INTERVAL_S, deadline - now))


async def _locked_job_state(conn: AsyncConnection, uid: UUID) -> str | None:
    """Return the job's current state under ``FOR UPDATE``, or None if the row is gone.

    Read inside the cancel transaction so the audited ``transition`` names the exact state
    ``update_state`` transitions from. ``queued``<->``running`` are both legal non-terminal
    edges, so a worker claim/requeue between the pre-authz read and the mutation could leave a
    cancel legal yet mislabel the prior state; holding the row lock from here through the update
    closes that window (#1083).
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT state FROM jobs WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return row[0] if row else None


async def cancel_job(pool: AsyncConnectionPool, ctx: RequestContext, job_id: str) -> ToolResponse:
    """Transition the job to ``canceled`` (cooperative); error on a terminal job.

    Cancelling a job that has already reached a terminal state is a no-op the agent
    must be able to act on, so the error envelope carries the job's actual current
    status in ``data["current_status"]`` (the agent learns *why* without a second
    ``jobs.get``). ``error_category`` stays paired with ``status="error"``, honoring
    the envelope's "category iff failure-status" invariant — the terminal lifecycle
    state goes in ``data``, not in ``status``.
    """
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    with bind_context(principal=ctx.principal, job_id=job_id):
        # Authorize before mutating: a job in an ungranted project must look absent and
        # never be canceled. The owning project never changes, so the read→update gap is
        # not an authz TOCTOU (the cancel still races a concurrent transition, which
        # update_state's IllegalTransition handles below).
        async with pool.connection() as conn:
            existing = await JOBS.get(conn, uid)
        if existing is None or not _in_scope(existing, ctx):
            return _not_found(job_id)
        denied = _require_job_role(existing, ctx, _cancel_role(existing.kind), job_id)
        if denied is not None:
            return denied
        try:
            async with pool.connection() as conn, conn.transaction():
                # Lock the row and read the true prior state before mutating, so the audited
                # transition names the state we actually cancel from (not the stale pre-authz
                # read — see _locked_job_state).
                prior_state = await _locked_job_state(conn, uid)
                job = await JOBS.update_state(conn, uid, JobState.CANCELED)
                # Audit the transition inside the mutation's transaction (ADR-0028): both commit
                # or neither does. The job kind rides the readable `transition` column — args is
                # stored one-way as args_digest, so a kind only there is not recoverable (#1083).
                await audit.record(
                    conn,
                    ctx,
                    audit.AuditEvent(
                        tool="jobs.cancel",
                        object_kind="jobs",
                        object_id=uid,
                        transition=f"{job.kind.value}:{prior_state}->canceled",
                        args={"job_id": job_id, "kind": job.kind.value},
                        project=_project(job),
                    ),
                )
        except ObjectNotFound:
            return _not_found(job_id)
        except IllegalTransition:
            async with pool.connection() as conn:
                current = await JOBS.get(conn, uid)
            data: dict[str, JsonValue] = {"current_status": current.state.value} if current else {}
            return ToolResponse.failure(job_id, ErrorCategory.CONFIGURATION_ERROR, data=data)
        return ToolResponse.from_job(job)


_JOBS_LIST_TAG = "jobs.list"


async def list_jobs(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    limit: int,
    cursor: str | None = None,
    status: JobState | None = None,
    kind: JobKind | None = None,
    investigation_id: str | None = None,
) -> ToolResponse:
    """Return a page of the newest jobs (keyset-paginated) in one collection envelope.

    Follows the ADR-0192 contract: fetches one row past ``limit`` to set
    ``data.truncated`` exactly and mints ``data.next_cursor`` from the last kept job's
    ``(created_at, id)``. A ``cursor`` from a prior page resumes strictly after it; a
    malformed or wrong-tool cursor is an ``invalid_cursor`` configuration error.

    Optional server-side filters (ADR-0197): ``status``/``kind`` narrow by lifecycle
    state / job kind; ``investigation_id`` narrows to the run-bearing jobs whose Run
    belongs to that Investigation (a malformed id is an ``invalid_uuid`` configuration
    error). Filters compose with the cursor — following ``next_cursor`` drains the full
    filtered set.
    """
    capped = _clamp_list_limit(limit)
    investigation_uid = None
    if investigation_id is not None:
        investigation_uid = _as_uuid(investigation_id)
        if investigation_uid is None:
            return _invalid_uuid_error("investigation_id", investigation_id)
    after = None
    if cursor:
        try:
            after = _decode_ts_uuid_cursor(_JOBS_LIST_TAG, cursor)
        except InvalidCursor:
            return _invalid_cursor_error("jobs")
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            jobs = await queue.recent_jobs(
                conn,
                capped + 1,
                _readable_projects(ctx),
                after=after,
                status=status,
                kind=kind,
                investigation_id=investigation_uid,
            )
        kept, truncated = _paginate(jobs, capped)
        next_cursor = (
            _encode_ts_uuid_cursor(_JOBS_LIST_TAG, kept[-1].created_at, kept[-1].id)
            if truncated and kept
            else None
        )
        responses = [_job_response(job) for job in kept]
        return ToolResponse.collection(
            "jobs",
            "ok",
            responses,
            suggested_next_actions=["jobs.get", "jobs.wait", "jobs.cancel"],
            data={"truncated": truncated, "next_cursor": next_cursor},
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the four `jobs.*` tools on ``app``, bound to ``pool``.

    Each wrapper resolves the request context (raising before the handler runs if no
    verified token reached the tool) and delegates; the handler owns its log context.
    """

    @app.tool(
        name="jobs.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def jobs_get(
        job_id: Annotated[str, Field(description="The Job to render.")],
    ) -> ToolResponse:
        """Return one durable job visible to the caller."""
        return await get_job(pool, current_context(), job_id)

    @app.tool(
        name="jobs.wait",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def jobs_wait(
        job_id: Annotated[str, Field(description="The Job to poll until terminal.")],
        timeout_s: Annotated[
            float,
            Field(
                description=(
                    "Seconds to wait before returning a non-terminal 'still running' result; "
                    f"defaults to {int(DEFAULT_WAIT_S)} and is capped at {int(MAX_WAIT_S)}. "
                    "Prefer the short default and repeated calls over a large value: a long "
                    "wait holds one request open long enough that an intermediary proxy may "
                    "sever the stream. Re-issue short waits rather than one long hold."
                )
            ),
        ] = DEFAULT_WAIT_S,
    ) -> ToolResponse:
        """Poll one durable job until it is terminal or the short timeout elapses.

        Returns as soon as the job reaches a terminal state (succeeded/failed/canceled)
        or ``timeout_s`` elapses, whichever comes first. A non-terminal return is normal,
        not an error: it carries the job's current (queued/running) status and lists
        ``jobs.wait`` in ``suggested_next_actions``, meaning "still running, call
        ``jobs.wait`` again". Re-issue short waits to poll a job to completion.

        Prefer many short waits over one long hold. An intermediary proxy can sever a
        long-held request as a raw transport drop (a socket close, not an error
        envelope). That drop is transient: retry the call. ``jobs.wait`` and the other
        ``jobs.*`` reads are idempotent, so retrying is safe.
        """
        return await wait_job(pool, current_context(), job_id, timeout_s)

    @app.tool(
        name="jobs.cancel",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def jobs_cancel(
        job_id: Annotated[str, Field(description="The Job to cancel.")],
    ) -> ToolResponse:
        """Cancel a queued or running job.

        A contributor may cancel their own leaseholder-lifecycle jobs (provision/reprovision/
        build/install/boot/power/authorize_ssh_key/…). Cancelling a destructive job
        (teardown/force_crash) requires operator.
        """
        return await cancel_job(pool, current_context(), job_id)

    @app.tool(
        name="jobs.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def jobs_list(
        request: Annotated[
            _JobsListPayload | None,
            Field(description="Jobs list filters and pagination request."),
        ] = None,
    ) -> ToolResponse:
        """List jobs visible to the caller, newest first, filterable by status/kind/investigation.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` to read the next page. Filters compose with the cursor.
        """
        request = request or _JobsListPayload()
        return await list_jobs(
            pool,
            current_context(),
            limit=request.limit,
            cursor=request.cursor,
            status=request.status,
            kind=request.kind,
            investigation_id=request.investigation_id,
        )
