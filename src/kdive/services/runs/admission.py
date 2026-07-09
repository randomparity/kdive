"""Run creation admission service for the `runs.create` boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn, cast
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.capacity.state import InvestigationState, RunState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.labels import validate_label
from kdive.domain.lifecycle.records import (
    Allocation,
    ExpectedBootFailure,
    Investigation,
    Run,
    System,
)
from kdive.domain.lifecycle.system_reuse import (
    ReuseRequirement,
    read_system_sizing,
    snapshot_satisfies,
)
from kdive.domain.profile_documents import SerializedExpectedBootFailure
from kdive.log import bind_context
from kdive.profiles.build import BuildProfile, dump_build_profile
from kdive.profiles.types import BuildProfileInput, ExpectedBootFailureInput
from kdive.providers.core.resolver import ProviderResolver
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.states import (
    ALLOC_HOSTABLE,
    INVESTIGATION_OPEN_FOR_RUN,
    RUN_HOSTABLE,
    RUN_NON_TERMINAL,
    SYSTEM_GONE,
)


@dataclass(frozen=True, slots=True)
class RunReuseRequirementInput:
    """Optional System snapshot assertions for reusing an existing System."""

    vcpus: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    pcie: list[str] | None = None

    def to_domain(self) -> ReuseRequirement:
        for field_name, value in (
            ("vcpus", self.vcpus),
            ("memory_gb", self.memory_gb),
            ("disk_gb", self.disk_gb),
        ):
            if value is not None and value <= 0:
                raise CategorizedError(
                    "reuse requirement sizing values must be positive",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"field": field_name},
                )
        return ReuseRequirement(
            vcpus=self.vcpus,
            memory_gb=self.memory_gb,
            disk_gb=self.disk_gb,
            pcie=self.pcie or [],
        )


@dataclass(frozen=True, slots=True)
class RunCreateRequest:
    """Validated transport input for creating a Run.

    ``system_id`` is optional (ADR-0169): omit it to create an unbound Run that commits to
    ``target_kind`` and is bound to a System later via ``runs.bind``. With a ``system_id`` the
    classic bound path runs and ``target_kind``, if given, must match the System's resource kind.
    """

    investigation_id: str
    build_profile: BuildProfileInput
    system_id: str | None = None
    target_kind: str | None = None
    expected_boot_failure: ExpectedBootFailureInput | None = None
    reuse_requirement: RunReuseRequirementInput | None = None
    label: str | None = None

    def domain_reuse_requirement(self) -> ReuseRequirement:
        if self.reuse_requirement is None:
            return ReuseRequirement()
        return self.reuse_requirement.to_domain()

    def object_id(self) -> str:
        """The id an error envelope keys on: the System for a bound Run, else the Investigation."""
        return self.system_id or self.investigation_id


# Failure reasons whose envelope is enriched with the registered `available_target_kinds`
# vocabulary at the tool boundary, so an agent that omitted or mistyped `target_kind` learns
# the valid set without a second call (ADR-0169 self-correcting error).
TARGET_KIND_VOCAB_REASONS = frozenset({"target_kind_required", "unknown_target_kind"})


class RunCreateError(CategorizedError):
    """Transport-neutral runs.create failure with the response object id preserved."""

    def __init__(
        self,
        object_id: str,
        message: str,
        *,
        category: ErrorCategory,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, category=category, details=details)
        self.object_id = object_id


@dataclass(frozen=True, slots=True)
class RunCreateResult:
    """Transport-neutral successful runs.create result."""

    run_id: UUID
    project: str
    investigation_id: UUID
    target_kind: ResourceKind
    system_id: UUID | None = None
    expected_boot_failure_kind: str | None = None
    label: str | None = None


type RunCreateRecorder = Callable[[AsyncConnection, RunCreateResult], Awaitable[None]]


async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunCreateRequest,
    *,
    resolver: ProviderResolver,
    recorder: RunCreateRecorder | None = None,
) -> RunCreateResult:
    """Create a Run, bound to a `ready` System or unbound against a ``target_kind`` (ADR-0169).

    With ``request.system_id`` the classic bound path runs (ADR-0026/0070): the System must be
    ``ready`` under an ``active`` Allocation, ``target_kind`` is derived from its resource kind,
    and an explicit mismatched ``target_kind`` is rejected. Without it the unbound path runs: a
    registered ``target_kind`` is required, no target capacity is held, and the Run is bound
    later via ``runs.bind``. ``request.reuse_requirement`` (bound path only) re-asserts the
    System sizing/PCIe under the lock, closing the list→create TOCTOU.

    ``recorder`` (idempotency, ADR-0193), when given, is awaited inside the Run-insert
    transaction with the freshly-built result, so the idempotency key and the Run commit
    atomically. It may raise (e.g. a key collision); the caller handles that.
    """
    object_id = request.object_id()
    try:
        label = validate_label(request.label)
    except CategorizedError as exc:
        _raise_from_error(object_id, exc)
    investigation_id = _parse_uuid(request.investigation_id)
    try:
        parsed_build_profile = BuildProfile.parse(request.build_profile)
    except CategorizedError as exc:
        _raise_from_error(object_id, exc)
    parsed_expected = _parse_expected_boot_failure(object_id, request.expected_boot_failure)
    try:
        requirement = request.domain_reuse_requirement()
    except CategorizedError as exc:
        _raise_from_error(object_id, exc)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            if request.system_id is None:
                return await _create_unbound(
                    conn,
                    ctx,
                    request,
                    investigation_id,
                    parsed_build_profile,
                    parsed_expected,
                    requirement=requirement,
                    resolver=resolver,
                    recorder=recorder,
                    label=label,
                )
            system_id = _parse_uuid(request.system_id)
            targets, project = await _resolve_targets(conn, ctx, investigation_id, system_id)
            return await _create_locked(
                conn,
                ctx,
                targets,
                parsed_build_profile,
                parsed_expected,
                project=project,
                requirement=requirement,
                explicit_target_kind=request.target_kind,
                recorder=recorder,
                label=label,
            )


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        _raise_config(value)


def _raise_from_error(object_id: str, exc: CategorizedError) -> NoReturn:
    raise _run_create_failure(object_id, exc) from exc


def _raise_config(
    object_id: str,
    *,
    detail: str = "invalid run creation request",
    data: dict[str, object] | None = None,
) -> NoReturn:
    raise _config_failure(object_id, detail=detail, data=data)


def _raise_stale(object_id: str, *, current_status: str) -> NoReturn:
    raise _stale_failure(object_id, current_status=current_status)


def _config_failure(
    object_id: str,
    *,
    detail: str = "invalid run creation request",
    data: dict[str, object] | None = None,
) -> RunCreateError:
    return RunCreateError(
        object_id,
        detail,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details=data,
    )


def _stale_failure(object_id: str, *, current_status: str) -> RunCreateError:
    return RunCreateError(
        object_id,
        "stale run creation target",
        category=ErrorCategory.STALE_HANDLE,
        details={"current_status": current_status},
    )


def _run_create_failure(object_id: str, exc: CategorizedError) -> RunCreateError:
    return RunCreateError(
        object_id,
        str(exc),
        category=exc.category,
        details=dict(exc.details),
    )


async def _resolve_targets(
    conn: AsyncConnection, ctx: RequestContext, investigation_id: UUID, system_id: UUID
) -> tuple[_CreateTargets, str]:
    """Pre-lock fetch + fast-fail checks; resolves the ALLOCATION lock key before locking.

    The allocation id must be known before the first lock (the global order acquires
    ALLOCATION before SYSTEM), so it is read here from the System and carried into the
    locked section, where the allocation is re-read under its lock as the authority.
    """
    inv = await INVESTIGATIONS.get(conn, investigation_id)
    if inv is None or inv.project not in ctx.projects:
        _raise_config(str(investigation_id))
    require_role(ctx, inv.project, Role.CONTRIBUTOR)
    system = await SYSTEMS.get(conn, system_id)
    if system is None or system.project not in ctx.projects:
        _raise_config(str(system_id))
    if system.project != inv.project:
        _raise_config(str(system_id))
    alloc = await ALLOCATIONS.get(conn, system.allocation_id)
    if alloc is None or alloc.state not in ALLOC_HOSTABLE:
        current = alloc.state.value if alloc is not None else "missing"
        _raise_stale(str(system_id), current_status=current)
    return _CreateTargets(
        investigation_id=investigation_id, system_id=system_id, allocation_id=alloc.id
    ), inv.project


def _parse_expected_boot_failure(
    object_id: str, value: ExpectedBootFailureInput | None
) -> SerializedExpectedBootFailure | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        _raise_config(object_id, data={"reason": "bad_expected_boot_failure"})
    try:
        parsed = ExpectedBootFailure.model_validate(value)
    except ValidationError:
        _raise_config(object_id, data={"reason": "bad_expected_boot_failure"})
    return cast(
        SerializedExpectedBootFailure,
        parsed.model_dump(mode="json", exclude_none=True),
    )


class _CreateTargets:
    """The three locked object ids for a ``runs.create``, carried into the locked section."""

    __slots__ = ("allocation_id", "investigation_id", "system_id")

    def __init__(self, *, investigation_id: UUID, system_id: UUID, allocation_id: UUID) -> None:
        self.investigation_id = investigation_id
        self.system_id = system_id
        self.allocation_id = allocation_id


async def _investigation_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def _resource_kind_for_system(conn: AsyncConnection, system_id: UUID) -> ResourceKind:
    """Return the resource kind backing a System (ADR-0169 bound-path target_kind derivation)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT r.kind FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "JOIN resources r ON r.id = a.resource_id "
            "WHERE s.id = %s",
            (system_id,),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: the System was validated live under its lock before this call.
        raise RuntimeError(f"resource kind lookup found no row for system {system_id}")
    return ResourceKind(row[0])


async def _count_non_terminal_runs(conn: AsyncConnection, system_id: UUID) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM runs WHERE system_id = %s AND state = ANY(%s)",
            (system_id, [s.value for s in RUN_NON_TERMINAL]),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(row[0])


def _system_block_error(system: System | None, system_id: UUID) -> RunCreateError | None:
    """Re-validate the System under the lock; return an error or ``None`` if ok."""
    if system is None:
        return _config_failure(str(system_id))
    if system.state in SYSTEM_GONE:
        return _stale_failure(str(system_id), current_status=system.state.value)
    if system.state not in RUN_HOSTABLE:
        return _config_failure(str(system_id), data={"current_status": system.state.value})
    return None


def _allocation_block_error(alloc: Allocation | None, system_id: UUID) -> RunCreateError | None:
    """Re-validate the Allocation under its lock (live + lease not lapsed), or ``None``.

    A terminal/expiring Allocation (non-``ACTIVE``, or ``ACTIVE`` whose ``lease_expiry`` has
    already elapsed — the ADR-0021 orphan-reaping window) is ``stale_handle`` (ADR-0070).
    """
    if alloc is None:
        return _stale_failure(str(system_id), current_status="missing")
    if alloc.state not in ALLOC_HOSTABLE:
        return _stale_failure(str(system_id), current_status=alloc.state.value)
    if alloc.lease_expiry is not None and alloc.lease_expiry < datetime.now(UTC):
        return _stale_failure(str(system_id), current_status="lease_expired")
    return None


async def _preconditions_block_response(
    conn: AsyncConnection,
    targets: _CreateTargets,
    *,
    project: str,
) -> tuple[RunCreateError, None] | tuple[None, tuple[System, Allocation]]:
    """Run the three unconditional preconditions under the held locks.

    Returns ``(failure, None)`` on a violation, else ``(None, (system, alloc))`` for the
    snapshot assertion and the insert to reuse. Order is fixed — System reachability, live
    allocation, single project, one-Run-per-System — so a stale/conflicting System returns
    its precondition error, never a sizing error.
    """
    system = await SYSTEMS.get(conn, targets.system_id)
    blocked = _system_block_error(system, targets.system_id)
    if blocked is not None or system is None:
        return blocked or _config_failure(str(targets.system_id)), None
    alloc = await ALLOCATIONS.get(conn, targets.allocation_id)
    blocked = _allocation_block_error(alloc, targets.system_id)
    if blocked is not None or alloc is None:
        return blocked or _stale_failure(str(targets.system_id), current_status="missing"), None
    if system.project != project:
        return _config_failure(str(targets.system_id)), None
    if await _count_non_terminal_runs(conn, targets.system_id) > 0:
        return (
            RunCreateError(
                str(targets.system_id),
                "system already has a live run",
                category=ErrorCategory.TRANSPORT_CONFLICT,
                details={"reason": "system_has_live_run"},
            ),
            None,
        )
    return None, (system, alloc)


def _assertion_block_response(
    system: System, alloc: Allocation, requirement: ReuseRequirement
) -> RunCreateError | None:
    """Apply the optional snapshot-≥ / pcie-contains assertion, or ``None`` if satisfied.

    Checked only after the three preconditions pass (so a stale/conflicting System never
    leaks sizing). A miss — or a malformed / ``class=`` pcie spec — is ``configuration_error``.
    """
    if requirement.is_empty():
        return None
    sizing = read_system_sizing(alloc, system)
    try:
        satisfied = snapshot_satisfies(sizing, alloc.pcie_claim, requirement)
    except CategorizedError as exc:
        return _run_create_failure(str(system.id), exc)
    if not satisfied:
        return _config_failure(str(system.id), data={"reason": "reuse_requirement_unmet"})
    return None


async def _create_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    targets: _CreateTargets,
    build_profile: BuildProfile,
    expected_boot_failure: SerializedExpectedBootFailure | None,
    *,
    project: str,
    requirement: ReuseRequirement,
    explicit_target_kind: str | None,
    recorder: RunCreateRecorder | None = None,
    label: str | None = None,
) -> RunCreateResult:
    # Global total lock order PROJECT < RESOURCE < ALLOCATION < SYSTEM, then INVESTIGATION →
    # RUN (locks.py, ADR-0040 §1): ALLOCATION must precede SYSTEM. The reconciler →expired
    # sweep and allocations.release both hold ...ALLOCATION before SYSTEM, so taking SYSTEM
    # first here would deadlock. Acquire ALLOCATION → SYSTEM → INVESTIGATION; the allocation
    # id is resolved pre-lock (create_run) and re-read under its lock as the authority.
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.ALLOCATION, targets.allocation_id),
        advisory_xact_lock(conn, LockScope.SYSTEM, targets.system_id),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, targets.investigation_id),
    ):
        blocked, ok = await _preconditions_block_response(conn, targets, project=project)
        if blocked is not None or ok is None:
            raise blocked or _config_failure(str(targets.system_id))
        system, alloc = ok
        assertion_block = _assertion_block_response(system, alloc, requirement)
        if assertion_block is not None:
            raise assertion_block
        inv = await _investigation_for_update(conn, targets.investigation_id)
        if inv is None:
            _raise_config(str(targets.investigation_id))
        if inv.state not in INVESTIGATION_OPEN_FOR_RUN:
            _raise_config(str(targets.investigation_id), data={"current_status": inv.state.value})
        target_kind = await _resource_kind_for_system(conn, targets.system_id)
        if explicit_target_kind is not None and explicit_target_kind != target_kind.value:
            raise _config_failure(
                str(targets.system_id),
                data={
                    "reason": "target_kind_mismatch",
                    "system_kind": target_kind.value,
                    "target_kind": explicit_target_kind,
                },
            )
        run = await _insert_run(
            conn,
            ctx,
            targets,
            build_profile,
            expected_boot_failure,
            project,
            target_kind=target_kind,
            label=label,
        )
        await _flip_investigation_if_open(conn, ctx, inv, targets.investigation_id, project)
        result = _created_result(run, expected_boot_failure, project)
        if recorder is not None:
            await recorder(conn, result)
    return result


async def _insert_run(
    conn: AsyncConnection,
    ctx: RequestContext,
    targets: _CreateTargets,
    build_profile: BuildProfile,
    expected_boot_failure: SerializedExpectedBootFailure | None,
    project: str,
    *,
    target_kind: ResourceKind,
    label: str | None = None,
) -> Run:
    now = datetime.now(UTC)
    run = await RUNS.insert(
        conn,
        Run(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            project=project,
            investigation_id=targets.investigation_id,
            system_id=targets.system_id,
            target_kind=target_kind,
            state=RunState.CREATED,
            build_profile=dump_build_profile(build_profile),
            expected_boot_failure=expected_boot_failure,
            label=label,
        ),
    )
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="runs.create",
            object_kind="runs",
            object_id=run.id,
            transition="->created",
            args={
                "investigation_id": str(targets.investigation_id),
                "system_id": str(targets.system_id),
            },
            project=project,
        ),
    )
    return run


async def _flip_investigation_if_open(
    conn: AsyncConnection,
    ctx: RequestContext,
    inv: Investigation,
    investigation_id: UUID,
    project: str,
) -> None:
    if inv.state is InvestigationState.OPEN:
        await INVESTIGATIONS.update_state(conn, investigation_id, InvestigationState.ACTIVE)
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="runs.create",
                object_kind="investigations",
                object_id=investigation_id,
                transition="open->active",
                args={"investigation_id": str(investigation_id)},
                project=project,
            ),
        )
    await conn.execute(
        "UPDATE investigations SET last_run_at = now() WHERE id = %s", (investigation_id,)
    )


def _created_result(
    run: Run,
    expected_boot_failure: SerializedExpectedBootFailure | None,
    project: str,
) -> RunCreateResult:
    kind = str(expected_boot_failure["kind"]) if expected_boot_failure is not None else None
    return RunCreateResult(
        run_id=run.id,
        project=project,
        investigation_id=run.investigation_id,
        target_kind=run.target_kind,
        system_id=run.system_id,
        expected_boot_failure_kind=kind,
        label=run.label,
    )


def _validate_unbound_target_kind(
    object_id: str, value: str | None, resolver: ProviderResolver
) -> ResourceKind:
    """Validate an unbound Run's ``target_kind`` against the registered provider kinds.

    The registered provider kinds are exactly the selectable target set. The
    ``available_target_kinds`` vocabulary is attached to the failure envelope at the tool
    boundary (it has the resolver), not embedded in the error details — ``safe_error_details``
    would drop the list anyway (ADR-0169).
    """
    if value is None:
        raise _config_failure(object_id, data={"reason": "target_kind_required"})
    try:
        kind = ResourceKind(value)
    except ValueError:
        kind = None
    if kind is None or kind not in resolver.registered_kinds():
        raise _config_failure(object_id, data={"reason": "unknown_target_kind"})
    return kind


async def _create_unbound(
    conn: AsyncConnection,
    ctx: RequestContext,
    request: RunCreateRequest,
    investigation_id: UUID,
    build_profile: BuildProfile,
    expected_boot_failure: SerializedExpectedBootFailure | None,
    *,
    requirement: ReuseRequirement,
    resolver: ProviderResolver,
    recorder: RunCreateRecorder | None = None,
    label: str | None = None,
) -> RunCreateResult:
    """Create a Run with no System (ADR-0169): commit a ``target_kind``, hold no capacity.

    Validates the target kind and the Investigation, and inserts the Run under the INVESTIGATION
    lock only — no Allocation or System is held, so no target capacity is debited. A
    ``reuse_requirement`` is meaningless without a System and is rejected.
    """
    object_id = request.investigation_id
    target_kind = _validate_unbound_target_kind(object_id, request.target_kind, resolver)
    if not requirement.is_empty():
        raise _config_failure(object_id, data={"reason": "reuse_requires_system"})
    inv = await INVESTIGATIONS.get(conn, investigation_id)
    if inv is None or inv.project not in ctx.projects:
        _raise_config(object_id)
    require_role(ctx, inv.project, Role.CONTRIBUTOR)
    project = inv.project
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
        locked_inv = await _investigation_for_update(conn, investigation_id)
        if locked_inv is None:
            _raise_config(object_id)
        if locked_inv.state not in INVESTIGATION_OPEN_FOR_RUN:
            _raise_config(object_id, data={"current_status": locked_inv.state.value})
        run = await _insert_unbound_run(
            conn,
            ctx,
            investigation_id,
            build_profile,
            expected_boot_failure,
            project,
            target_kind,
            label=label,
        )
        await _flip_investigation_if_open(conn, ctx, locked_inv, investigation_id, project)
        result = _created_result(run, expected_boot_failure, project)
        if recorder is not None:
            await recorder(conn, result)
    return result


async def _insert_unbound_run(
    conn: AsyncConnection,
    ctx: RequestContext,
    investigation_id: UUID,
    build_profile: BuildProfile,
    expected_boot_failure: SerializedExpectedBootFailure | None,
    project: str,
    target_kind: ResourceKind,
    *,
    label: str | None = None,
) -> Run:
    now = datetime.now(UTC)
    run = await RUNS.insert(
        conn,
        Run(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            project=project,
            investigation_id=investigation_id,
            system_id=None,
            target_kind=target_kind,
            state=RunState.CREATED,
            build_profile=dump_build_profile(build_profile),
            expected_boot_failure=expected_boot_failure,
            label=label,
        ),
    )
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="runs.create",
            object_kind="runs",
            object_id=run.id,
            transition="->created",
            args={
                "investigation_id": str(investigation_id),
                "target_kind": target_kind.value,
            },
            project=project,
        ),
    )
    return run
