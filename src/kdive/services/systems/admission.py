"""System define/provision admission service (ADR-0025).

`systems.provision` synchronously mints a System (state ``provisioning``) for a ``granted``
Allocation from a submitted profile, flips the Allocation ``granted -> active``, and enqueues a
``provision`` job. `systems.provision_defined` admits a `defined` System by System id after its
upload window is complete. Worker-owned ``provision``/``teardown``/``reprovision`` execution lives
in ``kdive.jobs.handlers.systems``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Literal, Protocol, Self
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.components.validation import ComponentSourceCapabilities
from kdive.config.core_settings import PROVISION_PREMUTATION_TIMEOUT_S
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Job, JobKind, System
from kdive.domain.sizing import MB_PER_GB, AllocationSizing
from kdive.domain.state import AllocationState, IllegalTransition, JobState, SystemState
from kdive.jobs import queue
from kdive.jobs.context import authorizing as job_authorizing
from kdive.jobs.payloads import SystemPayload
from kdive.profiles.provider_policy import reject_rootfs_upload_without_window
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    dump_profile,
    reconcile_profile_sizing,
    require_concrete_sizing,
)
from kdive.profiles.types import ProvisioningProfileInput
from kdive.providers.core.runtime import ProfilePolicy
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.serialization import safe_error_details
from kdive.services.systems.validation import (
    RootfsValidator,
    validate_profile_for_provider,
    validate_rootfs_for_provider,
)

# System states that occupy a per-project quota slot (terminal torn_down/failed do not).
_NON_TERMINAL_SYSTEM = (
    SystemState.DEFINED,  # the create-without-provision producer (systems.define)
    SystemState.PROVISIONING,
    SystemState.READY,
    SystemState.REPROVISIONING,
    SystemState.CRASHED,
)
type LockedAllocationSystem = tuple[AsyncConnection, Allocation, System | None]
type CreateSystemMode = Literal["provision", "define"]


class PreMutationTimeout(Protocol):
    """The async-context-manager timeout handle bounding the pre-mutation segment (ADR-0126).

    Structurally satisfied by :func:`asyncio.timeout`'s :class:`asyncio.Timeout`. The
    create-response builders call :meth:`reschedule` with ``None`` immediately before the
    first state-changing DB call, disabling the deadline so the mutation segment runs
    unbounded (a timeout there could orphan a mutation Python cannot kill).
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None: ...

    def reschedule(self, when: float | None) -> None: ...


type TimeoutFactory = Callable[[float], PreMutationTimeout]


@dataclass(frozen=True, slots=True)
class MissingAllocation:
    """A not-found or out-of-scope Allocation encountered while acquiring locks."""

    allocation_id: UUID


@dataclass(frozen=True, slots=True)
class CreateSystemRequest:
    allocation_id: UUID
    profile: ProvisioningProfileInput
    mode: CreateSystemMode


@dataclass(frozen=True, slots=True)
class ProvisionDefinedRequest:
    system_id: UUID


class AdmissionFailureReason(StrEnum):
    ALLOCATION_NOT_ADMITTED = "allocation_not_admitted"
    ALLOCATION_STATE_CONFLICT = "allocation_state_conflict"
    PROVIDER_POLICY_REJECTED = "provider_policy_rejected"
    QUOTA_EXCEEDED = "quota_exceeded"
    SUBJECT_NOT_FOUND = "subject_not_found"
    SYSTEM_ALREADY_DEFINED = "system_already_defined"
    SYSTEM_RECYCLE_REQUIRED = "system_recycle_required"
    SYSTEM_STATE_CONFLICT = "system_state_conflict"
    TIMEOUT = "timeout"


class AdmissionRecovery(StrEnum):
    INSPECT_SYSTEMS_AND_ALLOCATIONS = "inspect_systems_and_allocations"
    PROVISION_DEFINED_SYSTEM = "provision_defined_system"
    RECYCLE_ALLOCATION = "recycle_allocation"
    RETRY_PROVISION = "retry_provision"


@dataclass(frozen=True, slots=True)
class AdmissionFailure:
    subject_id: UUID
    category: ErrorCategory
    reason: AdmissionFailureReason
    current_status: str | None = None
    failure_message: str | None = None
    failure_details: dict[str, object] | None = None
    recovery: AdmissionRecovery | None = None


@dataclass(frozen=True, slots=True)
class ProvisionJobAdmitted:
    job: Job
    system_id: UUID


@dataclass(frozen=True, slots=True)
class DefinedSystemAdmitted:
    system: System


type AdmissionResult = AdmissionFailure | ProvisionJobAdmitted | DefinedSystemAdmitted


def _failure(
    subject_id: UUID,
    category: ErrorCategory = ErrorCategory.CONFIGURATION_ERROR,
    *,
    reason: AdmissionFailureReason,
    current_status: str | None = None,
    failure_message: str | None = None,
    failure_details: dict[str, object] | None = None,
    recovery: AdmissionRecovery | None = None,
) -> AdmissionFailure:
    return AdmissionFailure(
        subject_id=subject_id,
        category=category,
        reason=reason,
        current_status=current_status,
        failure_message=failure_message,
        failure_details=failure_details,
        recovery=recovery,
    )


def _failure_from_error(subject_id: UUID, exc: CategorizedError) -> AdmissionFailure:
    return _failure(
        subject_id,
        exc.category,
        reason=AdmissionFailureReason.PROVIDER_POLICY_REJECTED,
        failure_message=str(exc),
        failure_details=dict(safe_error_details(exc.details)),
    )


def _stored_profile_for(
    profile: ProvisioningProfileInput, alloc: Allocation
) -> ProvisioningProfile:
    """Resolve the concrete profile to store for ``alloc`` (ADR-0067, ADR-0024 delta).

    When the Allocation carries a complete resolved-sizing snapshot (``requested_vcpus`` /
    ``requested_memory_gb`` / ``requested_disk_gb``), the profile sizing is reconciled
    against it — filled when omitted, rejected when conflicting — so admitted size equals
    booted size. When the snapshot is incomplete (a full-custom or legacy allocation), the
    profile must carry its own concrete sizing. Either way the stored profile is concrete,
    so the libvirt renderer never reads a ``None`` size.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` on a conflicting restatement or a profile
            with missing sizing in the no-snapshot lane.
    """
    if (
        alloc.requested_vcpus is not None
        and alloc.requested_memory_gb is not None
        and alloc.requested_disk_gb is not None
    ):
        reconciled = reconcile_profile_sizing(
            profile,
            AllocationSizing(
                vcpu=alloc.requested_vcpus,
                memory_mb=alloc.requested_memory_gb * MB_PER_GB,
                disk_gb=alloc.requested_disk_gb,
            ),
        )
        return ProvisioningProfile.parse(reconciled)
    parsed = ProvisioningProfile.parse(profile)
    require_concrete_sizing(parsed)
    return parsed


@dataclass(frozen=True, slots=True)
class SystemAdmission:
    """Admission service with provider validation seams bound at construction.

    ``premutation_timeout_s`` overrides the configured pre-mutation bound (ADR-0126); when
    ``None`` the bound is read from ``KDIVE_PROVISION_PREMUTATION_TIMEOUT_S``.
    ``timeout_factory`` overrides the timeout context-manager constructor (defaulting to
    :func:`asyncio.timeout`); both exist as test seams and are ``None`` in production.
    """

    profile_policy: ProfilePolicy
    component_sources: ComponentSourceCapabilities
    rootfs_validator: RootfsValidator
    premutation_timeout_s: float | None = None
    timeout_factory: TimeoutFactory | None = None

    def _premutation_bound(self) -> float:
        if self.premutation_timeout_s is not None:
            return self.premutation_timeout_s
        return config.get(PROVISION_PREMUTATION_TIMEOUT_S) or 30.0

    def _timeout(self, bound: float) -> PreMutationTimeout:
        if self.timeout_factory is not None:
            return self.timeout_factory(bound)
        return asyncio.timeout(bound)

    async def provision_defined(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        request: ProvisionDefinedRequest,
    ) -> AdmissionResult:
        """Admit a ``defined`` System after its upload window is complete."""
        return await _provision_defined_locked(
            pool,
            ctx,
            request.system_id,
            profile_policy=self.profile_policy,
            component_sources=self.component_sources,
            rootfs_validator=self.rootfs_validator,
        )

    async def create_for_allocation(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        request: CreateSystemRequest,
    ) -> AdmissionResult:
        """Validate and lock the shared create-lane admission path.

        The submitted profile is structurally pre-parsed first (sizing now optional,
        ADR-0067) for early provider/rootfs validation. The sizing is reconciled against the
        Allocation's resolved snapshot inside the lock — once ``alloc`` is in scope — at the
        single create-insert point (:func:`_stored_profile_for`), so the stored profile is
        always concrete and admitted size equals booted size.
        """
        bound = self._premutation_bound()
        try:
            async with self._timeout(bound) as timeout:
                return await self._admit_within_bound(pool, ctx, request, timeout)
        except TimeoutError:
            # The pre-mutation segment exceeded the bound. The lock transaction rolled back on
            # cancellation, so no System/job was written (ADR-0126); convert the would-be socket
            # drop into a typed, retryable transport_failure. The retry is deduped by the
            # allocation lock (existing-System path), so retryable does not double-provision.
            return _failure(
                request.allocation_id,
                ErrorCategory.TRANSPORT_FAILURE,
                reason=AdmissionFailureReason.TIMEOUT,
                failure_message=(
                    f"provisioning admission exceeded the {bound:g}s pre-mutation bound; retry"
                ),
                recovery=AdmissionRecovery.RETRY_PROVISION,
            )
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, request.allocation_id)
            current_status = latest.state.value if latest else None
            return _failure(
                request.allocation_id,
                reason=AdmissionFailureReason.ALLOCATION_NOT_ADMITTED,
                current_status=current_status,
            )

    async def _admit_within_bound(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        request: CreateSystemRequest,
        timeout: PreMutationTimeout,
    ) -> AdmissionResult:
        """Run the bounded pre-mutation segment; the mutation disables the deadline (ADR-0126)."""
        try:
            parsed = ProvisioningProfile.parse(request.profile)
            validate_profile_for_provider(parsed, self.profile_policy, self.component_sources)
        except CategorizedError as exc:
            return _failure_from_error(request.allocation_id, exc)
        async with _locked_allocation_system(pool, ctx, request.allocation_id) as locked:
            if isinstance(locked, MissingAllocation):
                return _failure(
                    locked.allocation_id,
                    reason=AdmissionFailureReason.SUBJECT_NOT_FOUND,
                )
            conn, alloc, existing = locked
            try:
                stored = _stored_profile_for(request.profile, alloc)
            except CategorizedError as exc:
                return _failure_from_error(alloc.id, exc)
            if request.mode == "provision":
                return await _provision_create_response(
                    conn,
                    ctx,
                    alloc,
                    existing,
                    profile=stored,
                    profile_policy=self.profile_policy,
                    rootfs_validator=self.rootfs_validator,
                    timeout=timeout,
                )
            return await _define_create_response(
                conn,
                ctx,
                alloc,
                existing,
                profile=stored,
                profile_policy=self.profile_policy,
                rootfs_validator=self.rootfs_validator,
                timeout=timeout,
            )


async def _within_system_quota(conn: AsyncConnection, project: str) -> bool:
    """Report whether the project is under ``max_concurrent_systems`` (ADR-0007 §4).

    Fail-closed: a project with **no quota row** is over quota (no silent default).
    Counts the project's non-terminal Systems under the held PROJECT lock, so the
    count-then-create cannot overshoot under concurrent provisions.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT max_concurrent_systems FROM quotas WHERE project = %s", (project,)
        )
        row = await cur.fetchone()
    if row is None:
        return False
    cap = int(row[0])
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM systems WHERE project = %s AND state = ANY(%s)",
            (project, [s.value for s in _NON_TERMINAL_SYSTEM]),
        )
        count_row = await cur.fetchone()
    if count_row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(count_row[0]) < cap


async def _find_system_for_allocation(conn: AsyncConnection, alloc_id: UUID) -> System | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM systems WHERE allocation_id = %s ORDER BY created_at, id LIMIT 1",
            (alloc_id,),
        )
        row = await cur.fetchone()
    return System.model_validate(row) if row else None


@asynccontextmanager
async def _locked_allocation_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    alloc_id: UUID,
) -> AsyncIterator[LockedAllocationSystem | MissingAllocation]:
    # Resolve the allocation's project (immutable) before locking so the PROJECT lock key
    # is known up front; a missing/foreign allocation is a not-found-shaped config error.
    async with pool.connection() as probe:
        probe_alloc = await ALLOCATIONS.get(probe, alloc_id)
    if probe_alloc is None or probe_alloc.project not in ctx.projects:
        yield MissingAllocation(alloc_id)
        return
    project = probe_alloc.project
    # PROJECT → ALLOCATION (the global lock order, ADR-0040 §1): the project lock so the
    # max_concurrent_systems count-then-create is race-free against a concurrent provision,
    # the allocation lock so a release-mid-provision cannot leak a domain.
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, alloc_id),
    ):
        alloc = await ALLOCATIONS.get(conn, alloc_id)
        if alloc is None or alloc.project not in ctx.projects:
            yield MissingAllocation(alloc_id)
            return
        require_role(ctx, alloc.project, Role.OPERATOR)
        existing = await _find_system_for_allocation(conn, alloc_id)
        yield conn, alloc, existing


_FAILED_SYSTEM_GUIDANCE = (
    "this allocation's system is in 'failed' and cannot be re-provisioned; "
    "release this allocation and request a fresh one for a new system"
)


async def _failed_system_retry_failure(
    conn: AsyncConnection, alloc: Allocation, existing: System
) -> AdmissionFailure:
    """Build the actionable retry failure for a ``failed`` System (ADR-0149).

    Surfaces the original, already-worker-redacted provision reason (read from the failed
    provision job by its deterministic ``dedup_key``) alongside fixed recycle guidance, and
    names the release/re-request next actions. No re-mint: one System per Allocation. No new
    redaction — the worker already redacted ``failure_context``; this echoes those same bytes.
    """
    failure_message = _FAILED_SYSTEM_GUIDANCE
    failure_details: dict[str, object] = {}
    job = await queue.get_by_dedup_key(conn, f"{alloc.id}:provision")
    # Only a *failed* provision job carries the reason. A System can also reach `failed` via
    # `reprovisioning->failed`, leaving the original provision job `succeeded`; never advertise a
    # non-failed job as the failing one.
    if job is not None and job.state is JobState.FAILED:
        failure_details["failing_job_id"] = str(job.id)
        reason = job.failure_context.get("failure_message")
        if reason:
            failure_message = f"{_FAILED_SYSTEM_GUIDANCE} (original reason: {reason})"
        for key, value in job.failure_context.items():
            if key.startswith("failure_detail_"):
                failure_details[key] = value
    return _failure(
        existing.id,
        reason=AdmissionFailureReason.SYSTEM_RECYCLE_REQUIRED,
        current_status=existing.state.value,
        failure_message=failure_message,
        failure_details=failure_details,
        recovery=AdmissionRecovery.RECYCLE_ALLOCATION,
    )


async def _provision_create_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    existing: System | None,
    *,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
    timeout: PreMutationTimeout,
) -> AdmissionResult:
    if existing is None:
        return await _insert_provisioning_system(
            conn, ctx, alloc, profile, profile_policy, rootfs_validator, timeout
        )
    if existing.state is SystemState.DEFINED:
        return _failure(
            existing.id,
            reason=AdmissionFailureReason.SYSTEM_ALREADY_DEFINED,
            current_status=existing.state.value,
            recovery=AdmissionRecovery.PROVISION_DEFINED_SYSTEM,
        )
    if existing.state is SystemState.PROVISIONING:
        timeout.reschedule(None)  # mutation boundary: re-enqueue runs unbounded (ADR-0126)
        return await _enqueue_provision_job(
            conn,
            ctx,
            project=alloc.project,
            allocation_id=alloc.id,
            system_id=existing.id,
        )
    if existing.state is SystemState.FAILED:
        return await _failed_system_retry_failure(conn, alloc, existing)
    return _failure(
        existing.id,
        reason=AdmissionFailureReason.SYSTEM_RECYCLE_REQUIRED,
        current_status=existing.state.value,
        failure_message=_FAILED_SYSTEM_GUIDANCE,
        recovery=AdmissionRecovery.RECYCLE_ALLOCATION,
    )


async def _define_create_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    existing: System | None,
    *,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
    timeout: PreMutationTimeout,
) -> AdmissionResult:
    if existing is None:
        return await _insert_defined_system(
            conn, ctx, alloc, profile, profile_policy, rootfs_validator, timeout
        )
    if existing.state is SystemState.DEFINED:
        return DefinedSystemAdmitted(existing)  # idempotent re-define
    return _failure(
        existing.id,
        reason=AdmissionFailureReason.SYSTEM_STATE_CONFLICT,
        current_status=existing.state.value,
    )


async def _admit_defined(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    system: System,
) -> AdmissionResult:
    """Drive a ``defined`` System ``defined -> provisioning`` and enqueue its provision job.

    The stored profile is provisioned (ADR-0025 decision 7); the Allocation is already
    ``active`` (flipped at ``define``), so it is not touched. Keyed on the allocation, so
    a retried ``systems.provision`` dedups to the same job.
    """
    await SYSTEMS.update_state(conn, system.id, SystemState.PROVISIONING)
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="systems.provision",
            object_kind="systems",
            object_id=system.id,
            transition="defined->provisioning",
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    return await _enqueue_provision_job(
        conn,
        ctx,
        project=alloc.project,
        allocation_id=alloc.id,
        system_id=system.id,
    )


async def _enqueue_provision_job(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    project: str,
    allocation_id: UUID,
    system_id: UUID,
) -> ProvisionJobAdmitted:
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        SystemPayload(system_id=str(system_id)),
        job_authorizing(ctx, project),
        f"{allocation_id}:provision",
    )
    return ProvisionJobAdmitted(job=job, system_id=system_id)


async def _provision_defined_locked(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: UUID,
    *,
    profile_policy: ProfilePolicy,
    component_sources: ComponentSourceCapabilities,
    rootfs_validator: RootfsValidator,
) -> AdmissionResult:
    async with pool.connection() as probe:
        probe_system = await SYSTEMS.get(probe, system_id)
        if probe_system is None or probe_system.project not in ctx.projects:
            return _failure(system_id, reason=AdmissionFailureReason.SUBJECT_NOT_FOUND)
        project = probe_system.project
        allocation_id = probe_system.allocation_id
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, allocation_id),
    ):
        system = await SYSTEMS.get(conn, system_id)
        if system is None or system.project not in ctx.projects:
            return _failure(system_id, reason=AdmissionFailureReason.SUBJECT_NOT_FOUND)
        require_role(ctx, system.project, Role.OPERATOR)
        alloc = await ALLOCATIONS.get(conn, system.allocation_id)
        if alloc is None or alloc.project != system.project:
            return _failure(
                system.allocation_id,
                reason=AdmissionFailureReason.SUBJECT_NOT_FOUND,
            )
        return await _provision_defined_response(
            conn,
            ctx,
            system,
            alloc,
            profile_policy=profile_policy,
            component_sources=component_sources,
            rootfs_validator=rootfs_validator,
        )


async def _provision_defined_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    alloc: Allocation,
    *,
    profile_policy: ProfilePolicy,
    component_sources: ComponentSourceCapabilities,
    rootfs_validator: RootfsValidator,
) -> AdmissionResult:
    if system.state is SystemState.PROVISIONING:
        return await _enqueue_provision_job(
            conn,
            ctx,
            project=system.project,
            allocation_id=system.allocation_id,
            system_id=system.id,
        )
    if system.state is not SystemState.DEFINED:
        return _failure(
            system.id,
            reason=AdmissionFailureReason.SYSTEM_STATE_CONFLICT,
            current_status=system.state.value,
        )
    try:
        parsed = ProvisioningProfile.parse(system.provisioning_profile)
        validate_profile_for_provider(parsed, profile_policy, component_sources)
        await validate_rootfs_for_provider(parsed, profile_policy, rootfs_validator)
    except CategorizedError as exc:
        return _failure_from_error(system.id, exc)
    if alloc.state is not AllocationState.ACTIVE:
        return _failure(
            alloc.id,
            reason=AdmissionFailureReason.ALLOCATION_STATE_CONFLICT,
            current_status=alloc.state.value,
        )
    return await _admit_defined(conn, ctx, alloc, system)


async def _new_system_allowed(
    conn: AsyncConnection,
    alloc: Allocation,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
) -> AdmissionFailure | None:
    if alloc.state is not AllocationState.GRANTED:
        return _failure(
            alloc.id,
            reason=AdmissionFailureReason.ALLOCATION_STATE_CONFLICT,
            current_status=alloc.state.value,
        )
    # New System: enforce the per-project max_concurrent_systems quota under the held
    # project lock. Fail-closed — no quota row → denied (ADR-0007 §4); a denial writes
    # no System, no job, and leaves the allocation granted (the all-or-nothing rule).
    if not await _within_system_quota(conn, alloc.project):
        return _failure(
            alloc.id,
            ErrorCategory.QUOTA_EXCEEDED,
            reason=AdmissionFailureReason.QUOTA_EXCEEDED,
            recovery=AdmissionRecovery.INSPECT_SYSTEMS_AND_ALLOCATIONS,
        )
    try:
        await validate_rootfs_for_provider(profile, profile_policy, rootfs_validator)
    except CategorizedError as exc:
        return _failure_from_error(alloc.id, exc)
    return None


async def _insert_system_and_activate(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    *,
    state: SystemState,
    tool: str,
    transition: str,
) -> System:
    now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
    system = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            project=alloc.project,
            allocation_id=alloc.id,
            state=state,
            provisioning_profile=dump_profile(profile),
            shape=alloc.shape,
        ),
    )
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=tool,
            object_kind="systems",
            object_id=system.id,
            transition=transition,
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.ACTIVE)
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=tool,
            object_kind="allocations",
            object_id=alloc.id,
            transition="granted->active",
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    return system


async def _insert_defined_system(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
    timeout: PreMutationTimeout,
) -> AdmissionResult:
    blocked = await _new_system_allowed(conn, alloc, profile, profile_policy, rootfs_validator)
    if blocked is not None:
        return blocked
    timeout.reschedule(None)  # mutation boundary: the insert+activate runs unbounded (ADR-0126)
    system = await _insert_system_and_activate(
        conn,
        ctx,
        alloc,
        profile,
        state=SystemState.DEFINED,
        tool="systems.define",
        transition="->defined",
    )
    return DefinedSystemAdmitted(system)


async def _insert_provisioning_system(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
    timeout: PreMutationTimeout,
) -> AdmissionResult:
    try:
        reject_rootfs_upload_without_window(profile_policy, profile)
    except CategorizedError as exc:
        return _failure_from_error(alloc.id, exc)
    blocked = await _new_system_allowed(conn, alloc, profile, profile_policy, rootfs_validator)
    if blocked is not None:
        return blocked
    timeout.reschedule(None)  # mutation boundary: the insert+enqueue runs unbounded (ADR-0126)
    system = await _insert_system_and_activate(
        conn,
        ctx,
        alloc,
        profile,
        state=SystemState.PROVISIONING,
        tool="systems.provision",
        transition="->provisioning",
    )
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        SystemPayload(system_id=str(system.id)),
        job_authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return ProvisionJobAdmitted(job=job, system_id=system.id)
