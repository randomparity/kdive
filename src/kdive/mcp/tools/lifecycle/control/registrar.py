"""The `control.*` MCP tools (ADR-0028).

`control.power` (all actions ``on``/``off``/``cycle``/``reset`` → ``contributor``,
ADR-0320) admits only a ``READY`` System — a ``CRASHING`` (mid-force_crash) or ``CRASHED``
System holds crash evidence and is refused. `control.force_crash` (two-check gated, admin)
admits synchronously and enqueues a durable job. Worker-owned execution lives in
``kdive.jobs.handlers.control.control``; `power` moves no System state (a domain restart is not a
reprovision), while `force_crash` drives System ``ready -> crashing -> crashed`` (the
``crashing`` marker is set before the physical NMI so power cannot race it, ADR-0325) and every
non-terminal DebugSession of the System ``-> detached`` (joined through ``runs``).

`power` uses a per-call-unique ``dedup_key`` (``{system_id}:power:{action}:{uuid4}``) so a
repeated power op is always a fresh job; `force_crash` uses a stable
``{system_id}:force_crash`` key (once-per-System: one System per Allocation, no reprovision,
``ready -> crashing -> crashed`` is one-way).
"""

from __future__ import annotations

import math
from typing import Annotated
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, RUNS, SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import JobKind, PowerAction
from kdive.domain.operations.sysrq import SysRqCommand, parse_command
from kdive.jobs import queue
from kdive.jobs.payloads import (
    WATCH_DEFAULT_DEADLINE_S,
    WATCH_MAX_DEADLINE_S,
    CaptureTrafficPayload,
    PowerPayload,
    SysRqPayload,
    SystemPayload,
    WatchForCrashPayload,
)
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import (
    as_uuid as _as_uuid,
)
from kdive.mcp.tools._common import (
    authorizing as job_authorizing,
)
from kdive.mcp.tools._common import (
    authz_denied as _authz_denied,
)
from kdive.mcp.tools._common import (
    capability_unsupported as _capability_unsupported,
)
from kdive.mcp.tools._common import (
    config_error as _config_error,
)
from kdive.mcp.tools._common import (
    external_boot_denial as _external_boot_denial,
)
from kdive.mcp.tools._common import (
    invalid_uuid_error as _invalid_uuid_error,
)
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools.lifecycle.support._idempotency import dedup_replay, keyed_mutation
from kdive.mcp.tools.lifecycle.support._runtime_resolution import with_runtime_for_run
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security import audit
from kdive.security.artifacts.bpf_filter import hygiene_reason
from kdive.security.authz.context import RequestContext
from kdive.security.authz.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.authz.rbac import Role, require_role
from kdive.services.external_boot import (
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)

_FORCE_CRASH = JobKind.FORCE_CRASH
# Idempotency-store kinds (the registered tool names); ADR-0193.
_POWER_KIND = "control.power"
_FORCE_CRASH_KIND = "control.force_crash"
_DIAGNOSTIC_SYSRQ_KIND = "control.diagnostic_sysrq"
_WATCH_FOR_CRASH_KIND = "control.watch_for_crash"
_CAPTURE_TRAFFIC_KIND = "control.capture_traffic"
# `watch_for_crash` recycles a terminal-or-canceled watch into a fresh one, so only a live
# row is a replay. Named once: the probe and the enqueue must pass the same policy.
_WATCH_RECYCLE = queue.JobRecyclePolicy.TERMINAL_OR_CANCELED

# capture_traffic bounds (ADR-0385, #1258). Single source of truth for the tool's `Field`
# constraints and descriptions — interpolated into both so an agent never sees a hardcoded bound.
CAPTURE_MIN_DURATION_S = 1
CAPTURE_MAX_DURATION_S = 300
CAPTURE_DEFAULT_DURATION_S = 30
CAPTURE_MIN_BYTES = 1048576  # 1 MiB
CAPTURE_MAX_BYTES = 536870912  # 512 MiB
CAPTURE_DEFAULT_BYTES = 67108864  # 64 MiB
CAPTURE_MIN_SNAPLEN = 1
CAPTURE_MAX_SNAPLEN = 262144
CAPTURE_DEFAULT_SNAPLEN = 128
# The agent-facing allowlist rendered into the `command` Field description (single source of
# truth is `SysRqCommand`; ADR-0285).
_SYSRQ_COMMANDS = ", ".join(command.value for command in SysRqCommand)


async def power_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    action: str,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit a contributor power operation: READY for on/off/cycle/reset, PAUSED for resume.

    The project role, state, and external-boot admission gates run before a fresh enqueue.
    A restricting external boot activation refuses power operations. A supplied idempotency
    key replays its prior response; without one, each accepted call creates a distinct job.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    try:
        power_action = PowerAction(action)
    except ValueError:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.CONTRIBUTOR)
            # `resume` (ADR-0378) is admitted only from PAUSED (a start_paused restore's suspended
            # guest); every other action requires READY. So resume-from-READY and
            # on/off/cycle/reset from PAUSED/RESTORING are all refused.
            required = (
                SystemState.PAUSED if power_action is PowerAction.RESUME else SystemState.READY
            )
            if system.state is not required:
                return _config_error(system_id, data={"current_status": system.state.value})
            # A supplied key makes the power action idempotent by replacing the per-call uuid4
            # in the dedup key; absent, every call is a distinct power job (ADR-0193).
            dedup_suffix = idempotency_key if idempotency_key is not None else str(uuid4())

            async def _enqueue() -> ToolResponse:
                # Inside the closure, so `keyed_mutation`'s replay lookup runs first and only a
                # fresh enqueue is guarded: an activation must not un-idempotent a retry.
                try:
                    await check_external_boot_admission(
                        conn, uid, ExternalBootOperation.SYSTEM_POWER, project=system.project
                    )
                except ExternalBootDenied as exc:
                    return _external_boot_denial(system_id, exc, ctx)
                job = await queue.enqueue(
                    conn,
                    JobKind.POWER,
                    PowerPayload(system_id=system_id, action=power_action),
                    job_authorizing(ctx, system.project),
                    f"{system_id}:power:{power_action.value}:{dedup_suffix}",
                )
                return job_envelope(job, "system_id", uid)

            # SAVEPOINT, not a top-level transaction: `conn` already read the System above, so
            # this block defers to the request's own commit and holds the SYSTEM lock until then.
            # Nothing follows it in this handler, so the lock never spans later work.
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, uid):
                return await keyed_mutation(
                    conn,
                    idempotency_key=idempotency_key,
                    principal=ctx.principal,
                    project=system.project,
                    kind=_POWER_KIND,
                    do_work=_enqueue,
                )


async def _authorize_destructive(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    system_uid: UUID,
    op_kind: JobKind,
    *,
    resolver: ProviderResolver,
    tool: str,
) -> ToolResponse | None:
    allocation = await ALLOCATIONS.get(conn, system.allocation_id)
    if allocation is None or allocation.project not in ctx.projects:
        return _config_error(str(system_uid))
    op = DestructiveOp(
        kind=op_kind, profile_opt_in=await _op_opt_in(conn, system, op_kind, resolver)
    )
    try:
        assert_destructive_allowed(ctx, allocation, op)
    except DestructiveOpDenied as denied:
        async with conn.transaction():
            await audit.record(
                conn,
                ctx,
                audit.AuditEvent(
                    tool=tool,
                    object_kind="systems",
                    object_id=system_uid,
                    transition=f"{op_kind.value}:denied",
                    args={"system_id": str(system_uid), "missing": denied.missing},
                    project=system.project,
                ),
            )
        return _authz_denied(str(system_uid), denied.missing)
    return None


async def _op_opt_in(
    conn: AsyncConnection, system: System, op_kind: JobKind, resolver: ProviderResolver
) -> bool:
    """Resolve the gate's profile opt-in factor from the System's provisioning profile."""
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    runtime = await resolver.runtime_for_system(conn, system.id)
    return runtime.profile_policy.destructive_opt_in(profile, op_kind)


async def _optional_run_for_system(
    conn: AsyncConnection,
    ctx: RequestContext,
    run_id: str | None,
    system_id: UUID,
) -> UUID | None | ToolResponse:
    """Validate an optional readable Run is bound to the target System."""
    if run_id is None:
        return None
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    run = await RUNS.get(conn, uid)
    if run is None or run.project not in ctx.projects or run.system_id != system_id:
        return _config_error(run_id)
    require_role(ctx, run.project, Role.VIEWER)
    return uid


async def force_crash_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    resolver: ProviderResolver,
    run_id: str | None = None,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Gate, admit, and enqueue a `force_crash` job for a `ready` System (admin + gate).

    The in-project check precedes the gate, so the denial audit's ``project`` is always in
    ``ctx.projects`` and ``audit.record`` cannot itself raise (ADR-0028 ordering invariant).
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            run = await _optional_run_for_system(conn, ctx, run_id, uid)
            if isinstance(run, ToolResponse):
                return run
            gated = await _authorize_destructive(
                conn, ctx, system, uid, _FORCE_CRASH, resolver=resolver, tool="control.force_crash"
            )
            if isinstance(gated, ToolResponse):
                return gated
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})

            # Canonical uid (not the raw agent string, which UUID() accepts in non-canonical
            # forms): the reconciler's leak-recovery predicate matches this dedup_key against
            # `s.id::text` (canonical), so a non-canonical key would hide a live force_crash job
            # and trigger premature recovery (#1078). One expression, shared by the replay probe
            # and the enqueue, so the two cannot ask about different keys.
            dedup_key = f"{uid}:force_crash"

            async def _enqueue() -> ToolResponse:
                # Inside the closure, so `keyed_mutation`'s replay lookup runs first and only a
                # fresh enqueue is guarded: an activation must not un-idempotent a retry.
                # `f"{uid}:force_crash"` is stable across calls and recycles nothing, so an
                # unkeyed repeat returns the prior job unchanged. That must stay a replay: the
                # crash job is queued and will fire, and telling the agent it was refused
                # diverges what it believes from what the System is about to do.
                # Probed against the exact key the enqueue below uses, unconditionally. The
                # question is not whether a key was supplied but whether the key *varies* with
                # it: this one does not, so a fresh idempotency key cannot mint fresh work here
                # -- `queue.enqueue` returns the prior row either way -- and denying it would be
                # the same divergence as denying an unkeyed repeat (#2117 review).
                replay = await dedup_replay(conn, dedup_key)
                if replay is not None:
                    return job_envelope(replay, "system_id", uid)
                try:
                    await check_external_boot_admission(
                        conn,
                        uid,
                        ExternalBootOperation.FORCE_CRASH,
                        project=system.project,
                        run_id=run,
                    )
                except ExternalBootDenied as exc:
                    return _external_boot_denial(system_id, exc, ctx)
                job = await queue.enqueue(
                    conn,
                    JobKind.FORCE_CRASH,
                    SystemPayload(system_id=system_id),
                    job_authorizing(ctx, system.project),
                    dedup_key,
                )
                return job_envelope(job, "system_id", uid)

            # SAVEPOINT, not a top-level transaction: `conn` already read the System above, so
            # this block defers to the request's own commit and holds the SYSTEM lock until then.
            # Nothing follows it in this handler, so the lock never spans later work.
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, uid):
                return await keyed_mutation(
                    conn,
                    idempotency_key=idempotency_key,
                    principal=ctx.principal,
                    project=system.project,
                    kind=_FORCE_CRASH_KIND,
                    do_work=_enqueue,
                )


async def diagnostic_sysrq_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    command: str,
    resolver: ProviderResolver,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit a diagnostic SysRq on a ready, SysRq-capable System and enqueue the capture job.

    Non-destructive: requires ``contributor`` (no destructive-op gate), rejects an unknown or
    destructive ``command``, refuses a provider that does not advertise
    ``supports_diagnostic_sysrq`` with a ``capability_unsupported`` ``configuration_error``, and
    rejects a non-``ready`` System. The role check binds to the target System's project and runs
    after the in-project check, so it is never evaluated against a foreign project.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.CONTRIBUTOR)
            try:
                sysrq_command = parse_command(command)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(system_id, exc)
            binding = await resolver.binding_for_system(conn, system.id)
            if not binding.runtime.support.supports_diagnostic_sysrq:
                return _capability_unsupported(
                    system_id,
                    capability="diagnostic_sysrq",
                    provider=binding.runtime.support.component_sources.provider,
                    supported=[],
                )
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})
            dedup_suffix = idempotency_key if idempotency_key is not None else str(uuid4())

            async def _enqueue() -> ToolResponse:
                # Inside the closure, so `keyed_mutation`'s replay lookup runs first and only a
                # fresh enqueue is guarded: an activation must not un-idempotent a retry.
                try:
                    await check_external_boot_admission(
                        conn, uid, ExternalBootOperation.SYSTEM_SYSRQ, project=system.project
                    )
                except ExternalBootDenied as exc:
                    return _external_boot_denial(system_id, exc, ctx)
                job = await queue.enqueue(
                    conn,
                    JobKind.DIAGNOSTIC_SYSRQ,
                    SysRqPayload(system_id=system_id, command=sysrq_command),
                    job_authorizing(ctx, system.project),
                    f"{system_id}:diagnostic_sysrq:{sysrq_command.value}:{dedup_suffix}",
                )
                return job_envelope(job, "system_id", uid)

            # SAVEPOINT, not a top-level transaction: `conn` already read the System above, so
            # this block defers to the request's own commit and holds the SYSTEM lock until then.
            # Nothing follows it in this handler, so the lock never spans later work.
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, uid):
                return await keyed_mutation(
                    conn,
                    idempotency_key=idempotency_key,
                    principal=ctx.principal,
                    project=system.project,
                    kind=_DIAGNOSTIC_SYSRQ_KIND,
                    do_work=_enqueue,
                )


async def watch_for_crash_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    deadline_s: float,
    resolver: ProviderResolver,
    run_id: str | None = None,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit an out-of-band crash-signature console watch on a ready, crash-watch-capable System.

    Non-destructive: requires ``contributor``. ``deadline_s`` is validated (finite, positive) and
    clamped to ``WATCH_MAX_DEADLINE_S`` before enqueue, so a pure-wait watch cannot hold a worker
    slot past the cap. Refuses a provider that does not advertise ``supports_crash_watch`` with a
    ``capability_unsupported`` ``configuration_error``, and rejects a non-``ready`` System. The
    role check binds to the target System's project and runs after the in-project check, so it is
    never evaluated against a foreign project.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    if not math.isfinite(deadline_s) or deadline_s <= 0:
        return _config_error(system_id, data={"reason": "invalid_deadline"})
    clamped = min(deadline_s, WATCH_MAX_DEADLINE_S)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            run = await _optional_run_for_system(conn, ctx, run_id, uid)
            if isinstance(run, ToolResponse):
                return run
            require_role(ctx, system.project, Role.CONTRIBUTOR)
            binding = await resolver.binding_for_system(conn, system.id)
            if not binding.runtime.support.supports_crash_watch:
                return _capability_unsupported(
                    system_id,
                    capability="crash_watch",
                    provider=binding.runtime.support.component_sources.provider,
                    supported=[],
                )
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})

            # One expression for the probe and the enqueue below, so they cannot diverge.
            dedup_key = f"{system_id}:watch_for_crash"

            async def _enqueue() -> ToolResponse:
                # Stable per-System dedup key caps in-flight watches to one per System: a second
                # call while a watch is queued/running returns that same job (there is no reason to
                # watch one console twice at once), so a contributor cannot flood the shared worker
                # lane with unbounded pure-wait jobs — aggregate watch occupancy is bounded by the
                # quota-gated count of READY Systems. Terminal-or-canceled recycling lets a
                # re-issue after the prior watch completed *or was canceled* start a fresh watch (a
                # new reproducer batch) in place — without it a canceled watch would
                # wedge the stable slot forever and brick re-issue (the watch is
                # contributor-cancelable).
                #
                # The guard sits inside the closure, so `keyed_mutation`'s replay lookup runs
                # first and only a fresh enqueue is guarded: an activation must not un-idempotent
                # a retry.
                # The stable key described above is exactly a replay, so it is probed ahead of
                # the guard. Terminal-or-canceled rows are recycled into a fresh watch, so
                # `dedup_replay` correctly reports those as fresh work the matrix must decide.
                # Unconditional and against the exact key, for the reason given at
                # `force_crash`: this key does not vary with `idempotency_key` either.
                replay = await dedup_replay(conn, dedup_key, recycle=_WATCH_RECYCLE)
                if replay is not None:
                    return job_envelope(replay, "system_id", uid)
                try:
                    await check_external_boot_admission(
                        conn,
                        uid,
                        ExternalBootOperation.SYSTEM_WATCH_CRASH,
                        project=system.project,
                        run_id=run,
                    )
                except ExternalBootDenied as exc:
                    return _external_boot_denial(system_id, exc, ctx)
                job = await queue.enqueue(
                    conn,
                    JobKind.WATCH_FOR_CRASH,
                    WatchForCrashPayload(system_id=system_id, deadline_s=clamped),
                    job_authorizing(ctx, system.project),
                    dedup_key,
                    recycle=_WATCH_RECYCLE,
                )
                return job_envelope(job, "system_id", uid)

            # SAVEPOINT, not a top-level transaction: `conn` already read the System above, so
            # this block defers to the request's own commit and holds the SYSTEM lock until then.
            # Nothing follows it in this handler, so the lock never spans later work.
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, uid):
                return await keyed_mutation(
                    conn,
                    idempotency_key=idempotency_key,
                    principal=ctx.principal,
                    project=system.project,
                    kind=_WATCH_FOR_CRASH_KIND,
                    do_work=_enqueue,
                )


async def capture_traffic_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    resolver: ProviderResolver,
    run_id: str,
    duration_s: int,
    max_bytes: int,
    snaplen: int,
    capture_filter: str | None,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit a `capture_traffic` job for a Run whose bound System is a ready local-libvirt guest.

    Run-addressed (like ``vmcore.fetch``): ``with_runtime_for_run`` resolves the Run's bound
    provider runtime and applies the ``contributor`` role gate (an unbound Run or foreign project is
    refused there), then the inner path enforces the ``READY`` precondition, the provider's
    ``supports_traffic_capture`` capability, and BPF-filter hygiene. No job row is created on any
    rejection.
    """
    return await with_runtime_for_run(
        pool,
        resolver,
        ctx,
        run_id,
        lambda runtime: _capture_traffic(
            pool,
            ctx,
            run_id=run_id,
            duration_s=duration_s,
            max_bytes=max_bytes,
            snaplen=snaplen,
            capture_filter=capture_filter,
            runtime=runtime,
            idempotency_key=idempotency_key,
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _capture_traffic(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    duration_s: int,
    max_bytes: int,
    snaplen: int,
    capture_filter: str | None,
    runtime: ProviderRuntime,
    idempotency_key: str | None = None,
) -> ToolResponse:
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            if run.system_id is None:
                return _config_error(
                    run_id,
                    detail="run is not bound to a system; cannot capture traffic",
                    data={"reason": "run_unbound"},
                )
            system = await SYSTEMS.get(conn, run.system_id)
            if system is None:
                return _config_error(run_id)
            if system.state is not SystemState.READY:
                return _config_error(
                    run_id,
                    detail=(
                        "system must be in READY state to capture traffic; current state = "
                        f"{system.state.value}"
                    ),
                    data={"current_status": system.state.value},
                )
            if not runtime.support.supports_traffic_capture:
                return _capability_unsupported(
                    run_id,
                    capability="traffic_capture",
                    provider=runtime.support.component_sources.provider,
                    supported=[],
                )
            reason = hygiene_reason(capture_filter)
            if reason is not None:
                return _config_error(
                    run_id,
                    detail="capture filter is invalid",
                    data={"reason": "invalid_filter", "detail": reason},
                )

            # A Run owns many pcaps (one per capture, egressed by artifact_id), so a repeated call
            # must enqueue a fresh job — not replay the first like force_crash's once-per-System
            # key. Mirror control.power/diagnostic_sysrq: a supplied idempotency_key makes the call
            # replay-safe; absent, a per-call uuid4 makes every capture distinct.
            dedup_suffix = idempotency_key if idempotency_key is not None else str(uuid4())

            async def _enqueue() -> ToolResponse:
                # Inside the closure, so `keyed_mutation`'s replay lookup runs first and only a
                # fresh enqueue is guarded: an activation must not un-idempotent a retry.
                try:
                    await check_external_boot_admission(
                        conn,
                        system.id,
                        ExternalBootOperation.CAPTURE_TRAFFIC,
                        project=run.project,
                        run_id=uid,
                    )
                except ExternalBootDenied as exc:
                    return _external_boot_denial(run_id, exc, ctx)
                job = await queue.enqueue(
                    conn,
                    JobKind.CAPTURE_TRAFFIC,
                    CaptureTrafficPayload(
                        run_id=run_id,
                        duration_s=duration_s,
                        max_bytes=max_bytes,
                        snaplen=snaplen,
                        capture_filter=capture_filter,
                    ),
                    job_authorizing(ctx, run.project),
                    f"{run_id}:capture_traffic:{dedup_suffix}",
                )
                return job_envelope(job, "run_id", uid)

            # SAVEPOINT, not a top-level transaction: `conn` already read the System above, so
            # this block defers to the request's own commit and holds the SYSTEM lock until then.
            # Nothing follows it in this handler, so the lock never spans later work.
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system.id):
                return await keyed_mutation(
                    conn,
                    idempotency_key=idempotency_key,
                    principal=ctx.principal,
                    project=run.project,
                    kind=_CAPTURE_TRAFFIC_KIND,
                    do_work=_enqueue,
                )


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register the `control.*` tools on ``app``, bound to ``pool``."""

    _register_control_power(app, pool)
    _register_control_force_crash(app, pool, resolver)
    _register_control_diagnostic_sysrq(app, pool, resolver)
    _register_control_watch_for_crash(app, pool, resolver)
    _register_control_capture_traffic(app, pool, resolver)


def _register_control_power(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="control.power",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def control_power(
        system_id: Annotated[str, Field(description="The READY System to act on.")],
        action: Annotated[
            str,
            Field(
                description=(
                    "Power action: `on`/`off`/`cycle`/`reset`/`resume`. All require `contributor` "
                    "(leaseholder control over your transient VM). Use `reset`/`cycle` to recover "
                    "a wedged but READY guest. `on`/`off`/`cycle`/`reset` are admitted only on a "
                    "READY System (refused on a CRASHED/CRASHING/PAUSED System). `resume` is the "
                    "exception: it resumes a PAUSED System (left suspended by a `systems.restore` "
                    "with `start_paused=true`) back to READY, and is admitted only from PAUSED."
                )
            ),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Power action on a System: on/off/cycle/reset (READY only) or resume (PAUSED only).

        Requires contributor. A restricting external boot activation refuses every power action.
        When admitted, reset/cycle can recover a hung READY guest, and resume returns a System
        paused by systems.restore to READY. Preserve needed evidence before changing power.
        Returns a job handle; poll jobs.wait. Job success confirms the provider operation,
        not guest boot or SSH readiness.
        """
        return await power_system(
            pool,
            current_context(),
            system_id=system_id,
            action=action,
            idempotency_key=idempotency_key,
        )


def _register_control_force_crash(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="control.force_crash",
        annotations=_docmeta.destructive(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def control_force_crash(
        system_id: Annotated[str, Field(description="The ready System to force-crash via NMI.")],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
        run_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional Run bound to this System. Required while an active external boot "
                    "is owned by a Run; it must name that owner."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Request a deliberate panic through the provider; advance ready->crashing->crashed.

        Requires the project's admin role and profile destructive-operation opt-in. While an
        active external boot restricts the System, provide its owning run_id; another Run or
        no Run is refused. Other restricting activation states can refuse the operation.
        Returns a job handle; poll jobs.wait. This operation does not capture a vmcore, and its
        success alone does not prove the guest produced one. Check systems.get and console
        evidence before vmcore.fetch, then wait for capture success before analysis.
        """
        return await force_crash_system(
            pool,
            current_context(),
            system_id=system_id,
            resolver=resolver,
            run_id=run_id,
            idempotency_key=idempotency_key,
        )


def _register_control_diagnostic_sysrq(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="control.diagnostic_sysrq",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def control_diagnostic_sysrq(
        system_id: Annotated[
            str,
            Field(
                description=(
                    "The ready System to inspect (non-destructive). The bound provider must "
                    "support diagnostic SysRq injection."
                )
            ),
        ],
        command: Annotated[
            str,
            Field(
                description=(
                    "The diagnostic SysRq to inject. One of: "
                    f"{_SYSRQ_COMMANDS}. Destructive SysRq (crash/reboot/poweroff) is rejected — "
                    "use control.force_crash to crash a System."
                )
            ),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Send an allowed diagnostic SysRq to a READY guest and collect its console output.

        Requires contributor and provider diagnostic-SysRq support (local-libvirt and
        remote-libvirt). A restricting external boot activation refuses the operation.
        Returns a job handle; poll jobs.wait. On success refs.result is the redacted console
        artifact ID; read it with artifacts.get. Guest rejection or absent output fails the
        job with configuration_error. An unknown/destructive command, non-READY System, or
        unsupported provider is refused before enqueue.
        """
        return await diagnostic_sysrq_system(
            pool,
            current_context(),
            system_id=system_id,
            command=command,
            resolver=resolver,
            idempotency_key=idempotency_key,
        )


def _register_control_watch_for_crash(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="control.watch_for_crash",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def control_watch_for_crash(
        system_id: Annotated[
            str,
            Field(
                description=(
                    "The ready System whose console to watch. The bound provider must support "
                    "out-of-band crash-watch."
                )
            ),
        ],
        deadline_s: Annotated[
            float,
            Field(
                description=(
                    "Per-watch seconds measured by the worker monotonic clock after pickup. "
                    f"Defaults to {int(WATCH_DEFAULT_DEADLINE_S)}; larger values are clamped to "
                    f"{int(WATCH_MAX_DEADLINE_S)}. Returns not_fired at expiry if no signature "
                    "matched; submit a fresh watch request for another batch."
                )
            ),
        ] = WATCH_DEFAULT_DEADLINE_S,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
        run_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional Run bound to this System. Required while an active external boot "
                    "is owned by a Run; it must name that owner."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Watch a READY guest's serial console, returning on the first recognized signature.

        Requires contributor and provider crash-watch support (local-libvirt and remote-libvirt).
        While an active external boot restricts the System, provide its owning run_id; another
        Run or no Run is refused. Other restricting activation states can refuse the operation.
        Returns a job handle. Submit before starting the reproducer, then run the workload and
        poll jobs.wait concurrently; do not wait for the watch to finish before starting it.
        The console baseline is taken at worker pickup, so queue delay can exclude early output.

        After job success, parse refs.result JSON: outcome is fired (with signature, redacted
        matched text, and elapsed_s) or not_fired (no match during the observed window).
        Both outcomes are successful jobs. A signature may be non-fatal; neither outcome nor
        an SSH disconnect establishes guest state. Read console evidence with runs.get and the
        artifact tools. The watch does not mark the System CRASHED; check systems.get before
        vmcore.fetch. Only one watch is queued or running per System. For another batch, let
        it finish and submit a fresh request; reusing an idempotency key replays its response.
        A non-positive or non-finite deadline_s is refused before enqueue.
        """
        return await watch_for_crash_system(
            pool,
            current_context(),
            system_id=system_id,
            deadline_s=deadline_s,
            resolver=resolver,
            run_id=run_id,
            idempotency_key=idempotency_key,
        )


def _register_control_capture_traffic(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="control.capture_traffic",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def control_capture_traffic(
        run_id: Annotated[
            str,
            Field(
                description=(
                    "The Run whose bound ready System's traffic to capture. The bound provider "
                    "must support traffic capture."
                )
            ),
        ],
        duration_s: Annotated[
            int,
            Field(
                ge=CAPTURE_MIN_DURATION_S,
                le=CAPTURE_MAX_DURATION_S,
                description=(
                    "Capture window in seconds "
                    f"({CAPTURE_MIN_DURATION_S}-{CAPTURE_MAX_DURATION_S}); the job auto-stops when "
                    "it elapses. Cancel early with jobs.cancel."
                ),
            ),
        ] = CAPTURE_DEFAULT_DURATION_S,
        max_bytes: Annotated[
            int,
            Field(
                ge=CAPTURE_MIN_BYTES,
                le=CAPTURE_MAX_BYTES,
                description=(
                    "Stop early once the pcap reaches this many bytes "
                    f"({CAPTURE_MIN_BYTES}-{CAPTURE_MAX_BYTES})."
                ),
            ),
        ] = CAPTURE_DEFAULT_BYTES,
        snaplen: Annotated[
            int,
            Field(
                ge=CAPTURE_MIN_SNAPLEN,
                le=CAPTURE_MAX_SNAPLEN,
                description=(
                    "Bytes captured per packet "
                    f"({CAPTURE_MIN_SNAPLEN}-{CAPTURE_MAX_SNAPLEN}); the default "
                    f"{CAPTURE_DEFAULT_SNAPLEN} can include payload bytes as well as headers."
                ),
            ),
        ] = CAPTURE_DEFAULT_SNAPLEN,
        capture_filter: Annotated[
            str | None,
            Field(
                description=(
                    "Optional pcap-filter(7)/tcpdump BPF expression applied after capture "
                    "(e.g. 'tcp port 80'); the interface is fixed by the platform. Omit to keep "
                    "every captured packet."
                )
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Capture traffic from a Run's bound READY guest into a Run-owned pcap.

        Requires contributor and provider traffic-capture support. Local-libvirt captures the
        SSH-forward netdev; remote-libvirt captures the first aliased guest interface. This is
        not an all-interface capture. External-boot admission checks the Run's ownership and
        activation state before a fresh enqueue.

        Returns a job handle. Run the workload during capture, then poll jobs.wait. Collection
        stops after the worker's duration window, an observed size threshold, or cancellation.
        The optional capture_filter is applied after collection. snaplen limits bytes per
        packet and can retain payloads. On success refs.result is the pcap artifact ID; fetch
        it with artifacts.fetch_raw(run_id, asset="pcap", artifact_id=<refs.result>) for a
        presigned URL. artifacts.get does not serve these sensitive packet bytes inline.
        An empty pcap can be successful; cancellation does not promise a usable artifact.
        An unbound Run, non-READY System, or unsupported provider is refused before enqueue.
        Filter hygiene is checked before enqueue; invalid BPF syntax can instead fail the job
        when the worker validates it before starting capture.
        """
        return await capture_traffic_system(
            pool,
            current_context(),
            resolver=resolver,
            run_id=run_id,
            duration_s=duration_s,
            max_bytes=max_bytes,
            snaplen=snaplen,
            capture_filter=capture_filter,
            idempotency_key=idempotency_key,
        )
