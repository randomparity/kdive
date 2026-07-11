"""Run creation admission service for the `runs.create` boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
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
    ExpectedBootFailure,
    Investigation,
    Run,
)
from kdive.domain.lifecycle.system_reuse import ReuseRequirement
from kdive.domain.profile_documents import SerializedExpectedBootFailure
from kdive.log import bind_context
from kdive.profiles.build import BuildProfile, dump_build_profile
from kdive.profiles.types import BuildProfileInput, ExpectedBootFailureInput
from kdive.providers.core.resolver import ProviderResolver
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.serialization import JsonValue
from kdive.services.idempotency.envelope import StoredResult
from kdive.services.runs.host_admission import (
    RunHostTargets,
    check_host_preconditions,
    check_reuse_assertion,
    config_failure,
    parse_uuid,
    raise_config_error,
    raise_from_categorized_error,
    raise_stale_target,
    resource_kind_for_system,
)
from kdive.services.runs.states import (
    ALLOC_HOSTABLE,
    INVESTIGATION_OPEN_FOR_RUN,
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


def stored_run_create_result(result: RunCreateResult) -> StoredResult:
    """Serialize a successful Run creation result for idempotent replay."""
    document: dict[str, JsonValue] = {
        "type": "run_create",
        "run_id": str(result.run_id),
        "project": result.project,
        "investigation_id": str(result.investigation_id),
        "target_kind": result.target_kind.value,
        "system_id": str(result.system_id) if result.system_id is not None else None,
        "expected_boot_failure_kind": result.expected_boot_failure_kind,
        "label": result.label,
    }
    return StoredResult(document=document)


def run_create_result_from_stored(stored: StoredResult) -> RunCreateResult:
    """Deserialize a stored successful Run creation result."""
    if stored.document.get("type") != "run_create":
        raise CategorizedError(
            "stored run creation result is invalid",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"reason": "invalid_idempotency_result"},
        )
    system_id = stored.document["system_id"]
    return RunCreateResult(
        run_id=UUID(str(stored.document["run_id"])),
        project=str(stored.document["project"]),
        investigation_id=UUID(str(stored.document["investigation_id"])),
        target_kind=ResourceKind(str(stored.document["target_kind"])),
        system_id=UUID(str(system_id)) if isinstance(system_id, str) else None,
        expected_boot_failure_kind=_optional_str(stored.document["expected_boot_failure_kind"]),
        label=_optional_str(stored.document["label"]),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
        raise_from_categorized_error(object_id, exc)
    investigation_id = parse_uuid(request.investigation_id)
    try:
        parsed_build_profile = BuildProfile.parse(request.build_profile)
    except CategorizedError as exc:
        raise_from_categorized_error(object_id, exc)
    parsed_expected = _parse_expected_boot_failure(object_id, request.expected_boot_failure)
    try:
        requirement = request.domain_reuse_requirement()
    except CategorizedError as exc:
        raise_from_categorized_error(object_id, exc)
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
            system_id = parse_uuid(request.system_id)
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


async def _resolve_targets(
    conn: AsyncConnection, ctx: RequestContext, investigation_id: UUID, system_id: UUID
) -> tuple[RunHostTargets, str]:
    """Pre-lock fetch + fast-fail checks; resolves the ALLOCATION lock key before locking.

    The allocation id must be known before the first lock (the global order acquires
    ALLOCATION before SYSTEM), so it is read here from the System and carried into the
    locked section, where the allocation is re-read under its lock as the authority.
    """
    inv = await INVESTIGATIONS.get(conn, investigation_id)
    if inv is None or inv.project not in ctx.projects:
        raise_config_error(str(investigation_id))
    require_role(ctx, inv.project, Role.CONTRIBUTOR)
    system = await SYSTEMS.get(conn, system_id)
    if system is None or system.project not in ctx.projects:
        raise_config_error(str(system_id))
    if system.project != inv.project:
        raise_config_error(str(system_id))
    alloc = await ALLOCATIONS.get(conn, system.allocation_id)
    if alloc is None or alloc.state not in ALLOC_HOSTABLE:
        current = alloc.state.value if alloc is not None else "missing"
        raise_stale_target(str(system_id), current_status=current)
    return RunHostTargets(
        investigation_id=investigation_id, system_id=system_id, allocation_id=alloc.id
    ), inv.project


def _parse_expected_boot_failure(
    object_id: str, value: ExpectedBootFailureInput | None
) -> SerializedExpectedBootFailure | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise_config_error(object_id, data={"reason": "bad_expected_boot_failure"})
    try:
        parsed = ExpectedBootFailure.model_validate(value)
    except ValidationError:
        raise_config_error(object_id, data={"reason": "bad_expected_boot_failure"})
    return cast(
        SerializedExpectedBootFailure,
        parsed.model_dump(mode="json", exclude_none=True),
    )


async def _investigation_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def _create_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    targets: RunHostTargets,
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
        blocked, ok = await check_host_preconditions(conn, targets, project=project)
        if blocked is not None or ok is None:
            raise blocked or config_failure(str(targets.system_id))
        system, alloc = ok
        assertion_block = check_reuse_assertion(system, alloc, requirement)
        if assertion_block is not None:
            raise assertion_block
        inv = await _investigation_for_update(conn, targets.investigation_id)
        if inv is None:
            raise_config_error(str(targets.investigation_id))
        if inv.state not in INVESTIGATION_OPEN_FOR_RUN:
            raise_config_error(
                str(targets.investigation_id),
                data={"current_status": inv.state.value},
            )
        target_kind = await resource_kind_for_system(conn, targets.system_id)
        if explicit_target_kind is not None and explicit_target_kind != target_kind.value:
            raise config_failure(
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
    targets: RunHostTargets,
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
        raise config_failure(object_id, data={"reason": "target_kind_required"})
    try:
        kind = ResourceKind(value)
    except ValueError:
        kind = None
    if kind is None or kind not in resolver.registered_kinds():
        raise config_failure(object_id, data={"reason": "unknown_target_kind"})
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
        raise config_failure(object_id, data={"reason": "reuse_requires_system"})
    inv = await INVESTIGATIONS.get(conn, investigation_id)
    if inv is None or inv.project not in ctx.projects:
        raise_config_error(object_id)
    require_role(ctx, inv.project, Role.CONTRIBUTOR)
    project = inv.project
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
        locked_inv = await _investigation_for_update(conn, investigation_id)
        if locked_inv is None:
            raise_config_error(object_id)
        if locked_inv.state not in INVESTIGATION_OPEN_FOR_RUN:
            raise_config_error(object_id, data={"current_status": locked_inv.state.value})
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
