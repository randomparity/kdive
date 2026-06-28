"""``runs.build_install_boot`` — the build→install→boot composite (ADR-0267, #866).

One CONTRIBUTOR call that collapses the three job-bearing reproduce steps over an
already-created, already-bound Run, removing the per-step ``jobs.wait`` ceremony. The step
service functions only *enqueue* a job and return, so this composite supplies the blocking: per
phase it enqueues at the service layer (no MCP envelope re-entry) and polls that job to terminal
with the same ``JOBS.get``/terminal-state primitive ``jobs.wait`` loops on, then enqueues the
next phase, and finally reads the Run with ``get_run``.

Each phase is enqueued with a deterministic ``idempotency_key`` (``bib:<run_id>:<phase>``), so a
retried blocking call re-attaches to the in-flight jobs rather than double-enqueuing — which also
makes the composite **single-shot per Run**: once a phase's job is terminal (including failed),
re-calling returns the stored result, and retrying a transient failure is a granular-tool action
on the same ``run_id``. On the first non-``succeeded`` phase it returns ``data.failed_phase``;
on the ``timeout`` budget it returns the in-flight phase's running envelope (the jobs keep
running) so the agent reattaches with ``runs.get``/``jobs.list``. It does not retry or resume.

No mid-call MCP progress notification is emitted: kdive has no such tool pattern and the
transport's mid-call delivery is unverified (ADR-0267 plan). The bounded poll plus the
timeout→in-flight envelope is the agent's visibility mechanism instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import JOBS
from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run_target_kind
from kdive.mcp.tools.lifecycle.runs.server_build import build_handlers_for
from kdive.mcp.tools.lifecycle.runs.steps import boot_run, install_run
from kdive.mcp.tools.lifecycle.runs.view import get_run
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role

_PHASES = ("build", "install", "boot")
_TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED})
_POLL_INTERVAL_S = 2.0
#: Server cap (and default) on the single blocking call. The build dominates; on a longer build
#: the call returns the in-flight envelope and the agent reattaches.
COMPOSITE_TIMEOUT_MAX_S = 1800.0

type _Sleep = Callable[[float], Awaitable[None]]


async def _enqueue_phase(
    phase: str,
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    run_id: str,
) -> ToolResponse:
    """Enqueue one phase at the service layer with its deterministic idempotency key."""
    key = f"bib:{run_id}:{phase}"
    if phase == "install":
        return await install_run(pool, ctx, run_id, idempotency_key=key)
    if phase == "boot":
        return await boot_run(pool, ctx, run_id, idempotency_key=key)

    def _build(runtime: ProviderRuntime) -> Awaitable[ToolResponse]:
        return build_handlers_for(runtime).build_run(pool, ctx, run_id, idempotency_key=key)

    return await with_runtime_for_run_target_kind(
        pool, resolver, ctx, run_id, _build, required_role=Role.CONTRIBUTOR
    )


async def _poll_to_terminal(
    pool: AsyncConnectionPool, job_id: str, deadline: float, sleep: _Sleep
) -> tuple[Job | None, bool]:
    """Poll ``job_id`` until terminal or ``deadline``. Returns ``(job, timed_out)``.

    The same ``JOBS.get`` + terminal-state loop ``jobs.wait`` uses (holds no connection while
    sleeping). ``timed_out`` is True when the budget elapses before the job is terminal.
    """
    uid = UUID(job_id)
    loop = asyncio.get_running_loop()
    while True:
        async with pool.connection() as conn:
            job = await JOBS.get(conn, uid)
        if job is None:
            return None, False
        if job.state in _TERMINAL:
            return job, False
        if loop.time() >= deadline:
            return job, True
        await sleep(min(_POLL_INTERVAL_S, max(deadline - loop.time(), 0.0)))


def _tag(env: ToolResponse, extra: dict[str, JsonValue]) -> ToolResponse:
    """Return ``env`` with ``extra`` merged into its ``data`` (failed_phase/run_id markers)."""
    return env.model_copy(update={"data": {**env.data, **extra}})


async def build_install_boot(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    run_id: str,
    *,
    timeout: float | None = None,
    sleep: _Sleep = asyncio.sleep,
) -> ToolResponse:
    """Run build→install→boot over a bound Run, blocking each phase, and return ``runs.get``.

    On a phase enqueue precondition error, or a phase job that ends non-``succeeded``, returns
    that phase's envelope tagged ``data.failed_phase`` + ``run_id`` and stops. On ``timeout``
    budget exhaustion returns the in-flight phase's running envelope (jobs keep running) with
    ``runs.get``/``jobs.list`` reattach actions. On full success returns the terminal ``runs.get``
    projection.
    """
    loop = asyncio.get_running_loop()
    budget = (
        COMPOSITE_TIMEOUT_MAX_S
        if timeout is None
        else min(max(timeout, 0.0), COMPOSITE_TIMEOUT_MAX_S)
    )
    deadline = loop.time() + budget
    for phase in _PHASES:
        env = await _enqueue_phase(phase, pool, resolver, ctx, run_id)
        if env.status == "error":
            return _tag(env, {"failed_phase": phase, "run_id": run_id})
        job, timed_out = await _poll_to_terminal(pool, env.object_id, deadline, sleep)
        if job is None:
            return ToolResponse.failure(
                run_id,
                ErrorCategory.INFRASTRUCTURE_FAILURE,
                detail=f"{phase} job vanished after enqueue",
                data={"failed_phase": phase, "run_id": run_id},
            )
        base = ToolResponse.from_job(job)
        if timed_out:
            return base.model_copy(
                update={
                    "data": {**base.data, "in_flight_phase": phase, "run_id": run_id},
                    "suggested_next_actions": ["runs.get", "jobs.list"],
                }
            )
        if job.state is not JobState.SUCCEEDED:
            return base.model_copy(
                update={"data": {**base.data, "failed_phase": phase, "run_id": run_id}}
            )
    return await get_run(pool, ctx, run_id, resolver=resolver)


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register ``runs.build_install_boot`` (CONTRIBUTOR; phases enforce the role)."""

    @app.tool(
        name="runs.build_install_boot",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_build_install_boot(
        run_id: Annotated[
            str, Field(description="A created, bound, not-yet-built Run to drive to boot.")
        ],
        timeout: Annotated[
            float | None,
            Field(
                description="Max seconds to block before returning the in-flight phase to "
                "reattach via runs.get. Capped server-side; omit for the maximum.",
                gt=0,
            ),
        ] = None,
    ) -> ToolResponse:
        """Build, install, and boot a bound Run in one call; return the terminal runs.get."""
        return await build_install_boot(pool, resolver, current_context(), run_id, timeout=timeout)
