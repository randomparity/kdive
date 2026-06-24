"""Run binding admission for the `runs.bind` boundary (ADR-0169).

``runs.bind`` attaches a ready System to an unbound Run created on the decoupled path. It runs
the same System admission as the bound ``runs.create`` path — ready System, live Allocation,
single project, one-Run-per-System, optional reuse assertion — plus a kind-match contract: the
System's resource kind must equal the Run's committed ``target_kind``. The write is an
``IS NULL`` compare-and-set, so a concurrent double-bind loses harmlessly.

It reuses the System-admission helpers from :mod:`kdive.services.runs.admission` (the two
boundaries share one lock order and one precondition set); the underscore imports are an
intentional intra-package reuse, not a public-API dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import Run
from kdive.domain.lifecycle.system_reuse import ReuseRequirement
from kdive.log import bind_context
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.admission import (
    RunCreateError,
    RunReuseRequirementInput,
    _assertion_block_response,
    _config_failure,
    _CreateTargets,
    _parse_uuid,
    _preconditions_block_response,
    _raise_config,
    _raise_from_error,
    _raise_stale,
    _resource_kind_for_system,
    _stale_failure,
)
from kdive.services.runs.states import ALLOC_HOSTABLE, RUN_BINDABLE, RUN_BUILD_TERMINAL


@dataclass(frozen=True, slots=True)
class RunBindRequest:
    """Validated transport input for binding a Run to a System."""

    run_id: str
    system_id: str
    reuse_requirement: RunReuseRequirementInput | None = None

    def domain_reuse_requirement(self) -> ReuseRequirement:
        if self.reuse_requirement is None:
            return ReuseRequirement()
        return self.reuse_requirement.to_domain()


@dataclass(frozen=True, slots=True)
class RunBindResult:
    """Transport-neutral successful runs.bind result."""

    run_id: UUID
    system_id: UUID
    project: str


async def bind_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunBindRequest,
) -> RunBindResult:
    """Attach a ready System to an unbound Run (ADR-0169).

    Raises:
        RunCreateError: A precondition failed — the Run is already bound or terminal
            (``transport_conflict`` / ``stale_handle``), the System is not ready, the
            Allocation is not live, the System's kind does not match the Run's ``target_kind``,
            the System already hosts a live Run, or a reuse assertion is unmet.
    """
    run_id = _parse_uuid(request.run_id)
    system_id = _parse_uuid(request.system_id)
    try:
        requirement = request.domain_reuse_requirement()
    except CategorizedError as exc:
        _raise_from_error(request.run_id, exc)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            targets, project = await _resolve_bind_targets(conn, ctx, run_id, system_id)
            return await _bind_locked(
                conn, ctx, run_id, targets, project=project, requirement=requirement
            )


def _run_bindable_error(run: Run) -> RunCreateError | None:
    """Reject a Run that cannot be bound; the most specific reason wins (ADR-0169)."""
    if run.system_id is not None:
        return RunCreateError(
            str(run.id),
            "run is already bound to a system",
            category=ErrorCategory.TRANSPORT_CONFLICT,
            details={"reason": "run_already_bound"},
        )
    if run.state in RUN_BUILD_TERMINAL:
        return _stale_failure(str(run.id), current_status=run.state.value)
    if run.state not in RUN_BINDABLE:
        return _config_failure(str(run.id), data={"current_status": run.state.value})
    return None


async def _resolve_bind_targets(
    conn: AsyncConnection, ctx: RequestContext, run_id: UUID, system_id: UUID
) -> tuple[_CreateTargets, str]:
    """Pre-lock fetch + fast-fail; resolve the ALLOCATION lock key from the System.

    The Investigation is the Run's own; the Allocation id is read from the System so the locked
    section can take ALLOCATION before SYSTEM (the global order), re-reading each under its lock.
    """
    run = await RUNS.get(conn, run_id)
    if run is None or run.project not in ctx.projects:
        _raise_config(str(run_id))
    require_role(ctx, run.project, Role.CONTRIBUTOR)
    blocked = _run_bindable_error(run)
    if blocked is not None:
        raise blocked
    system = await SYSTEMS.get(conn, system_id)
    if system is None or system.project not in ctx.projects or system.project != run.project:
        _raise_config(str(system_id))
    alloc = await ALLOCATIONS.get(conn, system.allocation_id)
    if alloc is None or alloc.state not in ALLOC_HOSTABLE:
        current = alloc.state.value if alloc is not None else "missing"
        _raise_stale(str(system_id), current_status=current)
    targets = _CreateTargets(
        investigation_id=run.investigation_id, system_id=system_id, allocation_id=alloc.id
    )
    return targets, run.project


async def _bind_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    run_id: UUID,
    targets: _CreateTargets,
    *,
    project: str,
    requirement: ReuseRequirement,
) -> RunBindResult:
    # Lock order ALLOCATION < SYSTEM < INVESTIGATION < RUN (locks.py, ADR-0040 §1); the RUN lock
    # serializes bind against cancel, and the IS NULL compare-and-set is the final race backstop.
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.ALLOCATION, targets.allocation_id),
        advisory_xact_lock(conn, LockScope.SYSTEM, targets.system_id),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, targets.investigation_id),
        advisory_xact_lock(conn, LockScope.RUN, run_id),
    ):
        run = await RUNS.get(conn, run_id)
        if run is None:
            _raise_config(str(run_id))
        blocked = _run_bindable_error(run)
        if blocked is not None:
            raise blocked
        precond, ok = await _preconditions_block_response(conn, targets, project=project)
        if precond is not None or ok is None:
            raise precond or _config_failure(str(targets.system_id))
        system, alloc = ok
        system_kind = await _resource_kind_for_system(conn, targets.system_id)
        if system_kind != run.target_kind:
            raise _config_failure(
                str(run_id),
                data={
                    "reason": "target_kind_mismatch",
                    "system_kind": system_kind.value,
                    "target_kind": run.target_kind.value,
                },
            )
        assertion = _assertion_block_response(system, alloc, requirement)
        if assertion is not None:
            raise assertion
        if not await _bind_system(conn, run_id, targets.system_id):
            raise RunCreateError(
                str(run_id),
                "run is already bound to a system",
                category=ErrorCategory.TRANSPORT_CONFLICT,
                details={"reason": "run_already_bound"},
            )
        await _audit_bind(conn, ctx, run_id, targets, project)
    return RunBindResult(run_id=run_id, system_id=targets.system_id, project=project)


async def _bind_system(conn: AsyncConnection, run_id: UUID, system_id: UUID) -> bool:
    """Compare-and-set the binding; ``False`` means another writer bound the Run first."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE runs SET system_id = %s, updated_at = now() "
            "WHERE id = %s AND system_id IS NULL",
            (system_id, run_id),
        )
        return cur.rowcount == 1


async def _audit_bind(
    conn: AsyncConnection,
    ctx: RequestContext,
    run_id: UUID,
    targets: _CreateTargets,
    project: str,
) -> None:
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="runs.bind",
            object_kind="runs",
            object_id=run_id,
            transition="bind",
            args={
                "system_id": str(targets.system_id),
                "investigation_id": str(targets.investigation_id),
            },
            project=project,
        ),
    )
