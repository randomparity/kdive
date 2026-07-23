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
    invalid_uuid_error as _invalid_uuid_error,
)
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools._idempotency import keyed_mutation
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security import audit
from kdive.security.artifacts.bpf_filter import hygiene_reason
from kdive.security.authz.context import RequestContext
from kdive.security.authz.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.authz.rbac import Role, require_role

_FORCE_CRASH = JobKind.FORCE_CRASH
# Idempotency-store kinds (the registered tool names); ADR-0193.
_POWER_KIND = "control.power"
_FORCE_CRASH_KIND = "control.force_crash"
_DIAGNOSTIC_SYSRQ_KIND = "control.diagnostic_sysrq"
_WATCH_FOR_CRASH_KIND = "control.watch_for_crash"
_CAPTURE_TRAFFIC_KIND = "control.capture_traffic"

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
    """Admit a power op on a ``READY`` System and enqueue a `power` job.

    Every action (``on``/``off``/``cycle``/``reset``) requires ``contributor`` — leaseholder
    control over a transient VM (ADR-0320), not destructive administration. The role check
    binds to the target System's project and runs after the in-project check, so it cannot be
    evaluated against a foreign project. Admits only a ``READY`` System: a ``CRASHED`` (or any
    non-``READY``) System is refused with a ``configuration_error`` so crash evidence that
    ``capture_vmcore`` reads is not destroyed through the power path.
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
                job = await queue.enqueue(
                    conn,
                    JobKind.POWER,
                    PowerPayload(system_id=system_id, action=power_action),
                    job_authorizing(ctx, system.project),
                    f"{system_id}:power:{power_action.value}:{dedup_suffix}",
                )
                return job_envelope(job, "system_id", uid)

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


async def force_crash_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    resolver: ProviderResolver,
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
            gated = await _authorize_destructive(
                conn, ctx, system, uid, _FORCE_CRASH, resolver=resolver, tool="control.force_crash"
            )
            if isinstance(gated, ToolResponse):
                return gated
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})

            async def _enqueue() -> ToolResponse:
                job = await queue.enqueue(
                    conn,
                    JobKind.FORCE_CRASH,
                    SystemPayload(system_id=system_id),
                    job_authorizing(ctx, system.project),
                    # Canonical uid (not the raw agent string, which UUID() accepts in
                    # non-canonical forms): the reconciler's leak-recovery predicate matches this
                    # dedup_key against `s.id::text` (canonical), so a non-canonical key would hide
                    # a live force_crash job and trigger premature recovery (#1078).
                    f"{uid}:force_crash",
                )
                return job_envelope(job, "system_id", uid)

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
                job = await queue.enqueue(
                    conn,
                    JobKind.DIAGNOSTIC_SYSRQ,
                    SysRqPayload(system_id=system_id, command=sysrq_command),
                    job_authorizing(ctx, system.project),
                    f"{system_id}:diagnostic_sysrq:{sysrq_command.value}:{dedup_suffix}",
                )
                return job_envelope(job, "system_id", uid)

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

            async def _enqueue() -> ToolResponse:
                # Stable per-System dedup key caps in-flight watches to one per System: a second
                # call while a watch is queued/running returns that same job (there is no reason to
                # watch one console twice at once), so a contributor cannot flood the shared worker
                # lane with unbounded pure-wait jobs — aggregate watch occupancy is bounded by the
                # quota-gated count of READY Systems. `recycle_terminal`/`recycle_canceled` let a
                # re-issue after the prior watch completed *or was canceled* start a fresh watch (a
                # new reproducer batch) in place — without recycle_canceled a canceled watch would
                # wedge the stable slot forever and brick re-issue (the watch is
                # contributor-cancelable).
                job = await queue.enqueue(
                    conn,
                    JobKind.WATCH_FOR_CRASH,
                    WatchForCrashPayload(system_id=system_id, deadline_s=clamped),
                    job_authorizing(ctx, system.project),
                    f"{system_id}:watch_for_crash",
                    recycle_terminal=True,
                    recycle_canceled=True,
                )
                return job_envelope(job, "system_id", uid)

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
        """Power action on a System: on/off/cycle/reset (READY only) or resume (PAUSED only),
        all contributor-level leaseholder control. reset/cycle recover a wedged READY guest.
        resume returns a PAUSED System (left suspended by a start_paused systems.restore, for a
        gdbstub debug attach) to READY. on/off/cycle/reset are refused on a non-READY System (a
        CRASHED/CRASHING System holds crash evidence — use the crash workflow). Enqueues a power
        job."""
        return await power_system(
            pool,
            current_context(),
            system_id=system_id,
            action=action,
            idempotency_key=idempotency_key,
        )

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
    ) -> ToolResponse:
        """Inject an NMI to crash a ready System; drives ready->crashing->crashed.

        Requires admin + gate."""
        return await force_crash_system(
            pool,
            current_context(),
            system_id=system_id,
            resolver=resolver,
            idempotency_key=idempotency_key,
        )

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
        """Inject one non-destructive diagnostic SysRq into a ready guest and capture the kernel's
        console dump. The bound provider must support diagnostic SysRq injection (today
        local-libvirt and remote-libvirt); a provider that does not is refused with a
        `capability_unsupported` `configuration_error`. Requires contributor (no destructive gate);
        enqueues a job and returns `{job_id, status: queued}` — poll `jobs.wait`. On success the
        job's `refs.result` is the redacted console-dump artifact id; read it with `artifacts.get`.
        A guest that
        rejected the SysRq (`kernel.sysrq` restricts the operation) fails with a
        `configuration_error`, as does no console output at all (no keyboard driver); an
        unknown/destructive `command` or a non-ready System is also a `configuration_error`."""
        return await diagnostic_sysrq_system(
            pool,
            current_context(),
            system_id=system_id,
            command=command,
            resolver=resolver,
            idempotency_key=idempotency_key,
        )

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
                    "Seconds to watch the guest's serial console before returning a 'not fired' "
                    f"verdict; defaults to {int(WATCH_DEFAULT_DEADLINE_S)} and is clamped to "
                    f"{int(WATCH_MAX_DEADLINE_S)}. Size it to the reproducer batch you are about "
                    "to run; re-issue the watch for a longer campaign."
                )
            ),
        ] = WATCH_DEFAULT_DEADLINE_S,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Watch a ready guest's serial console out-of-band for a kernel-crash signature
        (panic/BUG/Oops/GPF/KASAN/KFENCE/soft-lockup) until `deadline_s`, returning on the first
        hit. The bound provider must support out-of-band crash-watch (today local-libvirt and
        remote-libvirt); a provider that does not is refused with a `capability_unsupported`
        `configuration_error`. Use this to catch a crash your own reproducer provokes: drive the
        repeat-until-crash loop over your root SSH, and this watches the console — which survives
        the panic that drops SSH. Requires contributor; enqueues a job and returns
        `{job_id, status: queued}` — poll `jobs.wait`, then read the verdict from the job's
        `refs.result`. The verdict's `outcome` is `fired` (a signature appeared: carries
        `signature`, a redacted matched `matched` slice, and `elapsed_s`) or `not_fired` (no
        signature before the deadline). Start the watch **before** you begin the reproducer loop
        so it does not miss an early crash; if your reproducer's SSH channel drops but the verdict
        is `not_fired`, the crash landed outside the watched window — read the full console with
        the `artifacts` tools. A non-ready System or a non-positive `deadline_s` is a
        `configuration_error`."""
        return await watch_for_crash_system(
            pool,
            current_context(),
            system_id=system_id,
            deadline_s=deadline_s,
            resolver=resolver,
            idempotency_key=idempotency_key,
        )

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
                    f"{CAPTURE_DEFAULT_SNAPLEN} captures headers only. Raise it to keep payloads."
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
        """Capture host-side network traffic from a Run's bound ready guest into a Run-owned pcap.
        The bound provider must support traffic capture (today local-libvirt); a provider that
        does not is refused with a `capability_unsupported` `configuration_error`. Only the guest's
        SSH-forward netdev is visible (the platform runs the guest on a restricted user-mode
        network), so this sees the traffic on that path, not arbitrary
        guest egress. Requires contributor; enqueues a fixed-duration job and returns
        `{job_id, status: queued}` — poll `jobs.wait`. On success the job's `refs.result` is the
        captured pcap's artifact id; the pcap is sensitive (packet bytes) and is fetched only with
        `artifacts.fetch_raw(run_id, asset="pcap", artifact_id=<refs.result>)`, which presigns a
        download URL — `artifacts.get` will not serve it. A 24-byte pcap means the capture saw zero
        packets. An unbound Run, a non-ready System, a provider that does not support capture, or an
        invalid `capture_filter` is a `configuration_error`; no job is created."""
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
