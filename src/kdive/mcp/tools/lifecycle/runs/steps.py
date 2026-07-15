"""`runs.install` and `runs.boot` MCP handlers."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.idempotency import delete_run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capacity.state import JobState, RunState
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.domain.lifecycle.run_steps import RUN_STEP_RUNNING, RUN_STEP_SUCCEEDED
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import InstallPayload, RunPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._idempotency import keyed_mutation
from kdive.mcp.tools.lifecycle.runs.common import run_job_envelope
from kdive.providers.core.resolver import ProviderResolver
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.steps import (
    build_baked_cmdline_extra,
    install_method_for,
    platform_owned_cmdline_token,
    step_progress,
)


def _crashkernel_error(run_id: str, crashkernel: str | None) -> ToolResponse | None:
    """Reject a malformed crashkernel reservation at the tool boundary (ADR-0300), else ``None``.

    Injection-safe, not range-validating: the token is opaque (a size or a multi-range), but a
    blank value, internal whitespace (which would inject an extra kernel token into the space-joined
    cmdline), a non-printable character (which would fail XML rendering of the domain
    ``<cmdline>``), or a leading ``crashkernel=`` prefix is rejected. Mirrors the ``InstallPayload``
    validator with per-reason codes for the synchronous tool response.
    """
    if crashkernel is None:
        return None
    stripped = crashkernel.strip()
    if not stripped:
        return _config_error(run_id, data={"reason": "crashkernel_blank"})
    if (
        stripped.split() != [stripped]
        or not stripped.isprintable()
        or stripped.lower().startswith("crashkernel=")
    ):
        return _config_error(run_id, data={"reason": "crashkernel_malformed"})
    return None


async def install_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    cmdline: str | None = None,
    crashkernel: str | None = None,
    resolver: ProviderResolver,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit an idempotent install for a built, SUCCEEDED Run.

    ``cmdline`` (ADR-0299, #988) is an optional boot-cmdline override applied against the
    already-built kernel: it **replaces** any build-time extra args (platform tokens are always
    preserved). ``crashkernel`` (ADR-0300, #989) is the optional kdump reservation size that tunes
    the platform ``crashkernel=<size>`` token (default 256M); it applies only to kdump-capture
    Systems. A value differing from the currently-installed one (either field) re-stages the boot
    without a rebuild; the same values are an idempotent no-op.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    owned = platform_owned_cmdline_token(cmdline)
    if owned is not None:
        return _config_error(
            run_id, data={"reason": "cmdline_overrides_platform_args", "token": owned}
        )
    if cmdline is not None and not cmdline.strip():
        return _config_error(run_id, data={"reason": "cmdline_blank"})
    crashkernel_error = _crashkernel_error(run_id, crashkernel)
    if crashkernel_error is not None:
        return crashkernel_error
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
            # The method gate is on the crashkernel path only: an install without a reservation is
            # byte-unchanged (no System fetch, no binding call, no new failure surface, ADR-0300).
            if crashkernel is not None:
                gate = await _reject_crashkernel_off_kdump(conn, run, resolver)
                if gate is not None:
                    return gate
            return await keyed_mutation(
                conn,
                idempotency_key=idempotency_key,
                principal=ctx.principal,
                project=run.project,
                kind="runs.install",
                do_work=lambda: _restage_and_enqueue_install(conn, ctx, run, cmdline, crashkernel),
            )


async def _reject_crashkernel_off_kdump(
    conn: AsyncConnection, run: Run, resolver: ProviderResolver
) -> ToolResponse | None:
    """Reject a crashkernel reservation on a non-kdump-family System, else ``None`` (ADR-0300).

    The kdump family is ``KDUMP`` and ``FADUMP`` (both reserve boot memory via ``crashkernel=``,
    ADR-0349); any other method rejects the reservation.

    Resolves the System's capture method (a cheap ``(kind, name)`` lookup plus in-process runtime
    construction — no libvirt round-trip). A resolution failure is mapped to ``configuration_error``
    rather than escaping as a 500. The install handler carries the same guard as a backstop for a
    hand-crafted payload or an accept-then-reprovision skew.
    """
    system_id = run.require_system_id()
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        return _config_error(str(run.id))
    try:
        binding = await resolver.binding_for_system(conn, system_id)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(str(run.id), exc)
    method = install_method_for(system, binding.runtime.profile_policy)
    if method not in (CaptureMethod.KDUMP, CaptureMethod.FADUMP):
        return _config_error(
            str(run.id), data={"reason": "crashkernel_requires_kdump", "method": method.value}
        )
    return None


async def _restage_and_enqueue_install(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    cmdline: str | None,
    crashkernel: str | None,
) -> ToolResponse:
    """Enqueue install, re-staging when the requested cmdline or crashkernel differs from installed.

    The whole decision — read the step ledger, delete the settled ``install``/``boot`` rows on a
    re-stage, and enqueue — runs inside one per-Run advisory-lock transaction so a concurrent
    ``runs.install`` cannot interleave read→delete→enqueue (ADR-0299/0300). The shared ledger-driven
    recycle (``_locked_enqueue``) then carries the new cmdline + crashkernel into the recycled job.
    """
    requested_cmdline = (
        cmdline.strip() if cmdline is not None else await build_baked_cmdline_extra(conn, run.id)
    )
    # Omit → default 256M (recorded as ``None``): each install fully specifies its variant, so an
    # omitted reservation reverts to the platform default, like the cmdline's build-baked anchor.
    requested_crashkernel = crashkernel.strip() if crashkernel is not None else None
    # Fold both fields into the audit args so a re-stage to a new variant is not audited the same as
    # the prior install (the args_digest is one-way — this distinguishes the operations without
    # making the values reverse-readable). Omitted when the field was not supplied.
    audit_args = {"run_id": str(run.id)}
    if cmdline is not None:
        audit_args["cmdline"] = cmdline.strip()
    if crashkernel is not None:
        audit_args["crashkernel"] = crashkernel.strip()
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        progress = await step_progress(conn, run.id)
        if progress.install == RUN_STEP_RUNNING or progress.boot == RUN_STEP_RUNNING:
            return _config_error(str(run.id), data={"reason": "step_in_progress"})
        variant_changed = (
            progress.installed_cmdline != requested_cmdline
            or progress.installed_crashkernel != requested_crashkernel
        )
        if progress.install == RUN_STEP_SUCCEEDED and variant_changed:
            await delete_run_step(conn, run.id, "install")
            await delete_run_step(conn, run.id, "boot")
        # The install envelope carries no ``replayed`` marker (boot-only, #1063); discard it.
        job, _ = await _locked_enqueue(
            conn,
            ctx,
            run,
            JobKind.INSTALL,
            "install",
            "runs.install",
            InstallPayload(run_id=str(run.id), cmdline=cmdline, crashkernel=crashkernel),
            audit_args,
        )
    return run_job_envelope(job, run.id)


async def boot_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    force: bool = False,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit an idempotent boot for a built, installed Run.

    Absent ``force``, a repeat call on an already-booted Run returns the prior job unchanged
    (``data.replayed=true``) and does not re-boot. ``force`` recycles the settled ``boot`` step
    so a fresh boot of the same installed variant runs without a re-stage (#1063). A ``force``
    call that reuses a prior ``idempotency_key`` replays the stored envelope — pass a distinct
    or no key to actually re-boot.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
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
                    force=force,
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
            "SELECT 1 FROM run_steps WHERE run_id = %s AND step = %s AND state = %s",
            (run_id, step, RUN_STEP_SUCCEEDED),
        )
        return await cur.fetchone() is not None


async def _has_step_row(conn: AsyncConnection, run_id: UUID, step: str) -> bool:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT 1 FROM run_steps WHERE run_id = %s AND step = %s", (run_id, step))
        return await cur.fetchone() is not None


# The job states ``queue.enqueue(recycle_terminal=...)`` resets in place; must match its
# ``state IN ('failed','succeeded')`` fence so the ``replayed`` marker tracks the actual reset.
_RECYCLABLE_JOB_STATES = frozenset({JobState.FAILED, JobState.SUCCEEDED})


async def _locked_enqueue(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    kind: JobKind,
    step: str,
    tool: str,
    payload: RunPayload,
    audit_args: dict[str, str],
) -> tuple[Job, bool]:
    """Enqueue a step job + record its audit, assuming the caller holds the per-Run lock.

    The recycle decision is ledger-driven (ADR-0299): a terminal job is recycled iff the step's
    ``run_steps`` row is absent. A present ``succeeded`` row is an idempotent no-op (the existing
    job is returned); a ``failed`` step's row was deleted by ``abandon_run_step`` so a retry
    recycles it; a re-stage (which deletes the row) likewise recycles, carrying the new payload.

    Returns the enqueued/returned :class:`Job` and ``replayed`` — ``True`` when ``queue.enqueue``
    returned a pre-existing job unchanged (no fresh work enqueued or recycled), ``False`` for a
    brand-new insert or an in-place terminal recycle. The boot path surfaces this as
    ``data.replayed`` (#1063). It reads the pre-existing job by dedup key rather than the
    ``run_steps`` row, because the row is written only when a **worker claims** the job — so a
    boot that is enqueued but not yet claimed (``queued``, no row) is a replay on a repeat call,
    which a row-presence proxy would miss.
    """
    dedup_key = f"{run.id}:{step}"
    recycle = not await _has_step_row(conn, run.id, step)
    prior = await queue.get_by_dedup_key(conn, dedup_key)
    job = await queue.enqueue(
        conn,
        kind,
        payload,
        job_authorizing(ctx, run.project),
        dedup_key,
        recycle_terminal=recycle,
    )
    # A prior job is reset in place only when ``recycle`` fires on a recyclable (terminal) state;
    # otherwise a prior job is returned unchanged (a replay) and an absent prior is a fresh insert.
    replayed = prior is not None and not (recycle and prior.state in _RECYCLABLE_JOB_STATES)
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=tool,
            object_kind="runs",
            object_id=run.id,
            transition=step,
            args=audit_args,
            project=run.project,
        ),
    )
    return job, replayed


async def _enqueue_step(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    kind: JobKind,
    step: str,
    tool: str,
    *,
    payload: RunPayload,
    force: bool = False,
) -> ToolResponse:
    """Enqueue a boot step job under the per-Run lock (the install path re-stages separately).

    ``force`` recycles a settled ``succeeded`` boot step so a fresh boot of the same installed
    variant runs without a re-stage (#1063): it deletes the ``run_steps`` row so the shared
    ledger-driven recycle resets the terminal boot job to a fresh ``queued`` attempt. A ``running``
    boot is rejected (``step_in_progress``) rather than recycled mid-flight, mirroring the
    ``runs.install`` re-stage guard. The returned envelope carries ``data.replayed``: ``True`` when
    a pre-existing job was returned unchanged (no fresh boot enqueued), ``False`` for a fresh or
    recycled boot.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        if force:
            progress = await step_progress(conn, run.id)
            if progress.boot == RUN_STEP_RUNNING:
                return _config_error(str(run.id), data={"reason": "step_in_progress"})
            if progress.boot == RUN_STEP_SUCCEEDED:
                await delete_run_step(conn, run.id, step)
        job, replayed = await _locked_enqueue(
            conn, ctx, run, kind, step, tool, payload, {"run_id": str(run.id)}
        )
    return run_job_envelope(job, run.id, replayed=replayed)
