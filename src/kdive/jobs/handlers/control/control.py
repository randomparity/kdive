"""Worker handlers for the `control.*` plane."""

from __future__ import annotations

import asyncio
from typing import Literal, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.lifecycle.rules import TERMINAL_SYSTEM_STATES
from kdive.domain.operations.jobs import Job, JobKind, PowerAction
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import PowerPayload, SystemPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security import audit
from kdive.services.debug.detach import detach_audit_event, detach_system_debug_sessions


class _ControlTarget(NamedTuple):
    domain_name: str
    project: str


def _resolved_domain_name(system: System) -> str:
    return system.domain_name or domain_name_for(system.id)


async def _power_target(
    conn: AsyncConnection, system_id: UUID, action: PowerAction
) -> _ControlTarget:
    """Resolve a power job's domain/project, re-checking the required state under the SYSTEM lock.

    Every action except ``resume`` requires ``READY`` (ADR-0320): a power job admitted while READY
    may dequeue after a ready->crashing/crashed transition, and a CRASHING (mid-force_crash) or
    CRASHED System holds crash evidence that must not be destroyed through the power path
    (ADR-0325). ``resume`` (ADR-0378) is the exception — it requires ``PAUSED`` (a start_paused
    restore's suspended guest) and is refused from every other state. ``terminal=True`` because the
    state will not improve on retry. (`force_crash` has its own precheck path; this is power-only.)
    """
    required = SystemState.PAUSED if action is PowerAction.RESUME else SystemState.READY
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "power target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state is not required:
            detail = (
                f"resume requires a {required.value} system"
                if action is PowerAction.RESUME
                else "power requires a READY system; crash evidence on a non-READY system is "
                "protected from the power path"
            )
            raise CategorizedError(
                detail,
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id), "current_status": system.state.value},
                terminal=True,
            )
        return _ControlTarget(_resolved_domain_name(system), system.project)


async def _controller(conn: AsyncConnection, system_id: UUID, resolver: ProviderResolver):
    """Resolve the System's controller port and tag the provider kind for metrics."""
    binding = await resolver.binding_for_system(conn, system_id)
    set_provider_kind(binding.kind.value)
    return binding.runtime.controller


async def power_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Drive the domain's power; audit `power:{action}`; move no System state."""
    payload = load_payload(job, PowerPayload)
    system_id = UUID(payload.system_id)
    action = payload.action
    target = await _power_target(conn, system_id, action)
    control = await _controller(conn, system_id, resolver)
    try:
        await asyncio.to_thread(control.power, target.domain_name, action)
    except CategorizedError:
        # A failed virDomainResume leaves the guest in an indeterminate power state, so PAUSED
        # is not a safe landing: route paused->failed (ADR-0378). Other actions move no state.
        if action is PowerAction.RESUME:
            await _fail_resume(conn, system_id)
        raise
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "power target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        # RESUME is the one power action that moves System state: paused->ready (ADR-0378).
        transition = f"power:{action.value}"
        if action is PowerAction.RESUME and system.state is SystemState.PAUSED:
            await SYSTEMS.update_state(conn, system_id, SystemState.READY)
            transition = "paused->ready"
        await audit.record(
            conn,
            job_context_from_job(job, target.project),
            audit.AuditEvent(
                tool="control.power",
                object_kind="systems",
                object_id=system_id,
                transition=transition,
                args={"system_id": str(system_id), "action": action.value},
                project=target.project,
            ),
        )
    return str(system_id)


async def _fail_resume(conn: AsyncConnection, system_id: UUID) -> None:
    """Route a PAUSED System to FAILED after a failed resume; tolerant of a raced state change."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is not None and system.state is SystemState.PAUSED:
            await SYSTEMS.update_state(conn, system_id, SystemState.FAILED)


_CrashAction = Literal["done", "finalize", "crash"]


async def _force_crash_precheck(conn: AsyncConnection, system_id: UUID) -> _CrashAction:
    """Classify a force_crash without transitioning, under the SYSTEM lock (ADR-0325).

    ``crash`` is the first attempt (READY): the caller resolves the controller, enters CRASHING,
    then fires the NMI. ``finalize`` is a retry whose CRASHING marker is already set: finalize
    only, no controller and no NMI (the marker means "NMI already dispatched"; re-firing into a
    mid-kdump guest can corrupt the dump). ``done`` is terminal or already CRASHED: nothing to do.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "force_crash target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state in TERMINAL_SYSTEM_STATES or system.state is SystemState.CRASHED:
            return "done"
        if system.state is SystemState.CRASHING:
            return "finalize"
        if system.state is SystemState.READY:
            return "crash"
        raise CategorizedError(
            "force_crash requires a READY system",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system_id), "current_status": system.state.value},
            terminal=True,
        )


async def _enter_crashing(conn: AsyncConnection, system_id: UUID) -> _ControlTarget | None:
    """Commit READY -> CRASHING under the lock, the last DB write before the NMI (ADR-0325).

    Returns ``None`` if the state moved out of READY between the precheck and here (a raced
    teardown), so the caller skips the NMI.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None or system.state is not SystemState.READY:
            return None
        await SYSTEMS.update_state(conn, system_id, SystemState.CRASHING)
        return _ControlTarget(_resolved_domain_name(system), system.project)


async def force_crash_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Crash the guest and drive System ready->crashing->crashed + DebugSession live->detached.

    The CRASHING marker is committed under the SYSTEM lock before the unlocked NMI so the power
    path's non-READY guard refuses the System for the whole NMI-to-CRASHED window (ADR-0325). A
    retry whose marker is already set finalizes without re-firing the NMI; an NMI-call raise
    propagates (the worker requeues) and is resolved evidence-first on retry / by the reconciler.
    """
    system_id = UUID(load_payload(job, SystemPayload).system_id)
    action = await _force_crash_precheck(conn, system_id)
    if action == "done":
        return str(system_id)
    if action == "finalize":
        await _finalize_force_crash(conn, job, system_id)
        return str(system_id)
    # First attempt: resolve the controller while still READY (a failure here leaves READY),
    # then commit CRASHING as the last DB write before the NMI.
    control = await _controller(conn, system_id, resolver)
    target = await _enter_crashing(conn, system_id)
    if target is None:
        return str(system_id)  # raced out of READY; nothing physical to do
    await asyncio.to_thread(control.force_crash, target.domain_name)
    await _finalize_force_crash(conn, job, system_id)
    return str(system_id)


async def _finalize_force_crash(conn: AsyncConnection, job: Job, system_id: UUID) -> None:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "force_crash target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state in TERMINAL_SYSTEM_STATES:
            return
        if system.state is SystemState.CRASHING:
            await SYSTEMS.update_state(conn, system_id, SystemState.CRASHED)
            await audit.record(
                conn,
                job_context_from_job(job, system.project),
                audit.AuditEvent(
                    tool="control.force_crash",
                    object_kind="systems",
                    object_id=system_id,
                    transition="crashing->crashed",
                    args={"system_id": str(system_id)},
                    project=system.project,
                ),
            )
        await detach_sessions(conn, job, system)


async def detach_sessions(conn: AsyncConnection, job: Job, system: System) -> None:
    """Drive every non-terminal DebugSession of ``system`` to detached (audited under the job)."""
    for session_id, old_state in await detach_system_debug_sessions(conn, system):
        await audit.record(
            conn,
            job_context_from_job(job, system.project),
            detach_audit_event(system, session_id, old_state),
        )


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
) -> None:
    """Bind the `power`/`force_crash` job handlers."""
    registry.register(JobKind.POWER, lambda conn, job: power_handler(conn, job, resolver=resolver))
    registry.register(
        JobKind.FORCE_CRASH,
        lambda conn, job: force_crash_handler(conn, job, resolver=resolver),
    )
