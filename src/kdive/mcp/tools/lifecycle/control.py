"""The `control.*` MCP tools (ADR-0028).

`control.power` (all actions ``on``/``off``/``cycle``/``reset`` → ``contributor``,
ADR-0320) admits only a ``READY`` System — a ``CRASHING`` (mid-force_crash) or ``CRASHED``
System holds crash evidence and is refused. `control.force_crash` (two-check gated, admin)
admits synchronously and enqueues a durable job. Worker-owned execution lives in
``kdive.jobs.handlers.control``; `power` moves no System state (a domain restart is not a
reprovision), while `force_crash` drives System ``ready -> crashing -> crashed`` (the
``crashing`` marker is set before the physical NMI so power cannot race it, ADR-0325) and every
non-terminal DebugSession of the System ``-> detached`` (joined through ``runs``).

`power` uses a per-call-unique ``dedup_key`` (``{system_id}:power:{action}:{uuid4}``) so a
repeated power op is always a fresh job; `force_crash` uses a stable
``{system_id}:force_crash`` key (once-per-System: one System per Allocation, no reprovision,
``ready -> crashing -> crashed`` is one-way).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import JobKind, PowerAction
from kdive.domain.operations.sysrq import SysRqCommand, parse_command
from kdive.jobs import queue
from kdive.jobs.payloads import PowerPayload, SysRqPayload, SystemPayload
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
    config_error as _config_error,
)
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools._idempotency import keyed_mutation
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.authz.rbac import Role, require_role

_FORCE_CRASH = JobKind.FORCE_CRASH
# Idempotency-store kinds (the registered tool names); ADR-0193.
_POWER_KIND = "control.power"
_FORCE_CRASH_KIND = "control.force_crash"
_DIAGNOSTIC_SYSRQ_KIND = "control.diagnostic_sysrq"
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
            if system.state is not SystemState.READY:
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
                    f"{system_id}:force_crash",
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
    """Admit a diagnostic SysRq on a ready local-libvirt System and enqueue the capture job.

    Non-destructive: requires ``contributor`` (no destructive-op gate), rejects an unknown or
    destructive ``command`` and any non-local-libvirt or non-``ready`` System with a
    ``configuration_error``. The role check binds to the target System's project and runs after
    the in-project check, so it is never evaluated against a foreign project.
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
            if binding.kind is not ResourceKind.LOCAL_LIBVIRT:
                return _config_error(
                    system_id,
                    detail="diagnostic SysRq is supported only on local-libvirt Systems",
                    data={"reason": "not_local_libvirt", "provider_kind": binding.kind.value},
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


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register the `control.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="control.power",
        annotations=_docmeta.destructive(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def control_power(
        system_id: Annotated[str, Field(description="The READY System to act on.")],
        action: Annotated[
            str,
            Field(
                description=(
                    "Power action: `on`/`off`/`cycle`/`reset`. All require `contributor` "
                    "(leaseholder control over your transient VM). Use `reset`/`cycle` to "
                    "recover a wedged but READY guest. Admitted only on a READY System; "
                    "refused on a CRASHED or CRASHING (mid-force_crash) System."
                )
            ),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Power action on a READY System: on/off/cycle/reset, all contributor-level
        leaseholder control. reset/cycle recover a wedged READY guest. Refused on a
        non-READY System (a CRASHED or CRASHING System holds crash evidence — use the crash
        workflow). Enqueues a power job."""
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
            str, Field(description="The ready local-libvirt System to inspect (non-destructive).")
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
        """Inject one non-destructive diagnostic SysRq into a ready local-libvirt guest and
        capture the kernel's console dump. Requires contributor (no destructive gate); enqueues
        a job and returns `{job_id, status: queued}` — poll `jobs.wait`. On success the job's
        `refs.result` is the redacted console-dump artifact id; read it with `artifacts.get`. A
        guest that rejected the SysRq (`kernel.sysrq` restricts the operation) fails with a
        `configuration_error`, as does no console output at all (no keyboard driver); an
        unknown/destructive `command`, a non-local-libvirt System, or a non-ready System is also
        a `configuration_error`."""
        return await diagnostic_sysrq_system(
            pool,
            current_context(),
            system_id=system_id,
            command=command,
            resolver=resolver,
            idempotency_key=idempotency_key,
        )
