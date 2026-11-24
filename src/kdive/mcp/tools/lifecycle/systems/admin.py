"""Destructive system administration MCP handlers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.components.validation import ComponentSourceCapabilities
from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import (
    IllegalTransition,
    JobState,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import ReprovisionPayload, TeardownPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse, validate_stored
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import authz_denied as _authz_denied
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import external_boot_denial as _external_boot_denial
from kdive.mcp.tools._common import job_envelope, teardown_routing_failure
from kdive.mcp.tools._common import stale_handle as _stale_handle
from kdive.mcp.tools.lifecycle.support._device_ops import device_job_active, device_op_in_flight
from kdive.mcp.tools.lifecycle.support._idempotency import (
    dedup_replay,
    object_scoped_kind,
    record_envelope,
    resolve_conflict,
    resolve_envelope_replay,
    validate_idempotency_key,
)
from kdive.profiles.provider_policy import (
    ProfilePolicy,
    require_investigation_binding_for_upload,
)
from kdive.profiles.provisioning import ProvisioningProfile, dump_profile, profile_digest
from kdive.profiles.types import ProvisioningProfileInput
from kdive.providers.core.resolver import ProviderResolver
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, RoleDenied, require_role
from kdive.services.external_boot import (
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)
from kdive.services.investigations.common import TERMINAL_INVESTIGATION_STATES
from kdive.services.systems.admission import require_pinned_cpu_selectable
from kdive.services.systems.authority_owned import (
    AuthoritySystemBinding,
    authority_system_binding,
    enqueue_preactivation_teardown,
)
from kdive.services.systems.teardown import (
    TeardownRoutingDenied,
    route_external_boot_teardown,
)
from kdive.services.systems.validation import (
    RootfsValidator,
    validate_profile_for_provider,
    validate_rootfs_for_provider,
)

_NON_TERMINAL_RUN = frozenset({RunState.CREATED, RunState.RUNNING})
_LIVE_JOB_STATES = frozenset({JobState.QUEUED, JobState.RUNNING})
_TEARDOWN = JobKind.TEARDOWN
# Idempotency-store kinds (the registered tool names); ADR-0193.
_REPROVISION_KIND = "systems.reprovision"
_TEARDOWN_KIND = "systems.teardown"
_EXTERNAL_BOOT_ACTIVATIONS = ExternalBootActivationRepository()
# A ready System's re-admission of a profile it applied before recycles that profile's settled
# dedup row (#3008), and a recycle resets the attempt counter, which a still-running handler of
# the old row would then pass at its heartbeat and finalize fences. So the row blocks while it is
# live, or for ADR-0634's 15-minute settle window after a write that can hide a running handler:
# `canceled` (`jobs.cancel` leaves the handler running), `lease_expired`, or any attempt past the
# first (a reclaim of a lapsed attempt). The stalled-reprovision lane defers on the same rows
# (ADR-0435, #2980 amendment); a `succeeded` reclaim is added here because only a recycle reuses
# its attempt numbers.
_REPROVISION_SETTLE = timedelta(minutes=15)
_REPROVISION_SETTLING_SQL: LiteralString = (
    "SELECT id FROM jobs WHERE dedup_key = %s AND (state = ANY(%s) "
    "    OR ((state = %s OR error_category = %s OR attempt > 1) AND updated_at > now() - %s))"
)


def _reprovision_key_kind(system_id: UUID, profile: ProvisioningProfile) -> str:
    """The reprovision key's kind: the System and the parsed profile's digest (ADR-0193).

    The digest is over the parsed profile, so an omitted profile field and its explicit default
    are one request, and a changed one is another.
    """
    return object_scoped_kind(_REPROVISION_KIND, system_id, {"profile": profile_digest(profile)})


def _teardown_dedup_key(system_id: UUID) -> str:
    """One expression for the replay probe and the enqueue; the key does not vary with
    ``idempotency_key``, so the probe runs unconditionally."""
    return f"{system_id}:teardown"


@dataclass(frozen=True, slots=True)
class SystemAdminHandlers:
    """Destructive system handlers with provider validation seams bound at construction."""

    profile_policy: ProfilePolicy
    component_sources: ComponentSourceCapabilities
    rootfs_validator: RootfsValidator

    async def reprovision_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        system_id: str,
        profile: ProvisioningProfileInput,
        idempotency_key: str | None = None,
    ) -> ToolResponse:
        """Reprovision a `ready` System in place under the same Allocation."""
        uid = _as_uuid(system_id)
        if uid is None:
            return _config_error(system_id)
        try:
            parsed = ProvisioningProfile.parse(profile)
            validate_profile_for_provider(parsed, self.profile_policy, self.component_sources)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(system_id, exc)
        if idempotency_key is not None:
            try:
                validate_idempotency_key(idempotency_key)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error("idempotency_key", exc)
        kind = _reprovision_key_kind(uid, parsed)
        with bind_context(principal=ctx.principal):
            if idempotency_key is not None:
                async with pool.connection() as conn:
                    replay = await resolve_envelope_replay(
                        conn, principal=ctx.principal, key=idempotency_key, kind=kind
                    )
                if replay is not None:
                    return replay
            try:
                return await _reprovision_locked(
                    pool,
                    ctx,
                    uid,
                    parsed,
                    self.profile_policy,
                    self.rootfs_validator,
                    idempotency_key=idempotency_key,
                    idempotency_kind=kind,
                )
            except IllegalTransition:
                async with pool.connection() as conn:
                    latest = await SYSTEMS.get(conn, uid)
                data = {"current_status": latest.state.value} if latest else {}
                return _config_error(system_id, data=data)


async def _reprovision_locked(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: UUID,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
    *,
    idempotency_key: str | None = None,
    idempotency_kind: str,
) -> ToolResponse:
    try:
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
        ):
            return await _reprovision_in_lock(
                conn,
                ctx,
                system_id,
                profile,
                profile_policy,
                rootfs_validator,
                idempotency_key,
                idempotency_kind,
            )
    except UniqueViolation:
        if idempotency_key is None:
            raise  # only the keyed path records, so an unkeyed collision is not ours
        async with pool.connection() as conn:
            try:
                return await resolve_conflict(
                    conn,
                    principal=ctx.principal,
                    key=idempotency_key,
                    kind=idempotency_kind,
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error("idempotency_key", exc)


async def _reprovision_in_lock(
    conn: AsyncConnection,
    ctx: RequestContext,
    system_id: UUID,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
    idempotency_key: str | None,
    idempotency_kind: str,
) -> ToolResponse:
    system = await SYSTEMS.get(conn, system_id)
    if system is None or system.project not in ctx.projects:
        return _config_error(str(system_id))
    allocation = await ALLOCATIONS.get(conn, system.allocation_id)
    if allocation is None or allocation.project not in ctx.projects:
        return _config_error(str(system_id))
    # Reprovision is contributor leaseholder lifecycle (ADR-0326): re-staging your own granted
    # System is iterating, not administering. Enforced in-handler (the handler-direct path
    # bypasses the registrar gate), mirroring control.power.
    require_role(ctx, system.project, Role.CONTRIBUTOR)
    try:
        # An upload-rootfs reprovision resolves the base within the System's investigation, so the
        # System must already carry a write-once binding (ADR-0441 §2); reject a missing one here
        # rather than let the worker fetch fail late.
        require_investigation_binding_for_upload(profile_policy, profile, system.investigation_id)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(str(system_id), exc)
    if system.investigation_id is not None:
        # A reprovision re-materializes the base under the System's investigation, so it must not
        # begin under a closed/abandoned one (ADR-0441 §7). Take the INVESTIGATION lock
        # close_investigation holds so a reprovision and a close serialize: the close sees this
        # System (and blocks or force-reaps it), or this read sees the close and rejects. The xact
        # lock is held to commit, covering the ready->reprovisioning transition, not only the read.
        async with advisory_xact_lock(conn, LockScope.INVESTIGATION, system.investigation_id):
            investigation = await INVESTIGATIONS.get(conn, system.investigation_id)
        if investigation is None or investigation.state in TERMINAL_INVESTIGATION_STATES:
            state = investigation.state.value if investigation is not None else "missing"
            return _config_error(str(system_id), data={"investigation_state": state})
    if await authority_system_binding(conn, system_id) is not None:
        return ToolResponse.failure(
            str(system_id),
            ErrorCategory.CONFLICT,
            detail="authority-owned Systems cannot be reprovisioned in place",
            suggested_next_actions=["systems.teardown", "systems.get"],
            data={"reason": "authority_system_reprovision_unsupported"},
        )
    digest = profile_digest(profile)
    dedup_key = f"{system_id}:reprovision:{digest}"
    if system.state is SystemState.REPROVISIONING:
        existing = await _job_for_dedup_key(conn, dedup_key)
        if existing is not None:
            return job_envelope(existing, "system_id", system_id)
        return _config_error(str(system_id), data={"current_status": system.state.value})
    if await _EXTERNAL_BOOT_ACTIVATIONS.teardown_authority_is_current(conn, system_id):
        return ToolResponse.failure(
            str(system_id),
            ErrorCategory.CONFLICT,
            detail="System teardown is already in progress",
            suggested_next_actions=["systems.get"],
            data={"reason": "external_boot_teardown_in_progress"},
        )
    # Below the `REPROVISIONING` replay return above: a repeat call that finds the live dedup job
    # enqueues nothing and returns it unchanged, so it is a poll rather than fresh work, and an
    # activation that appeared since must not turn it into a `conflict` while that job stays
    # queued and runs. Still ahead of every write — `_admit_reprovision` is the first.
    try:
        await check_external_boot_admission(
            conn, system_id, ExternalBootOperation.SYSTEM_REPROVISION, project=system.project
        )
    except ExternalBootDenied as exc:
        return _external_boot_denial(str(system_id), exc, ctx)
    if system.state is not SystemState.READY:
        return _config_error(str(system_id), data={"current_status": system.state.value})
    # An ordinary teardown enqueue leaves the System `ready`, so only its job row shows it is
    # pending (#2979). A settled row is safe: the teardown handler re-checks state under this lock.
    # Below the READY check, so a System the teardown already moved keeps its `current_status`.
    teardown = await _job_for_dedup_key(conn, _teardown_dedup_key(system_id))
    if teardown is not None and teardown.state in _LIVE_JOB_STATES:
        return ToolResponse.failure(
            str(system_id),
            ErrorCategory.CONFLICT,
            detail="System teardown is queued or running; check systems.get before reprovisioning",
            suggested_next_actions=["systems.get"],
            data={"reason": "teardown_in_progress"},
        )
    if await device_job_active(conn, system_id):
        return device_op_in_flight(str(system_id))
    if await _has_live_run(conn, system_id):
        return _stale_handle(str(system_id), current_status=system.state.value)
    if (settling := await _settling_reprovision_job(conn, dedup_key)) is not None:
        return ToolResponse.failure(
            str(system_id),
            ErrorCategory.CONFLICT,
            detail="a prior reprovision job for this profile may still be running; "
            "retry once it settles",
            suggested_next_actions=["systems.get"],
            data={"reason": "reprovision_job_settling", "job_id": str(settling)},
        )
    try:
        await validate_rootfs_for_provider(profile, profile_policy, rootfs_validator)
        # A reprovision can carry a new cpu.model pin; validate it against the bound host's
        # selectable_cpus fail-closed BEFORE the ready->reprovisioning transition (ADR-0369), so an
        # undeliverable pin is rejected pre-mutation exactly as on first provision — never destroy a
        # working System by rendering a custom <cpu> the host cannot deliver. Only load the bound
        # Resource when a pin is present (the common pin-less reprovision skips the round-trip).
        section = profile.provider.local_libvirt_section
        if section is not None and section.cpu is not None:
            resource = (
                await RESOURCES.get(conn, allocation.resource_id)
                if allocation.resource_id is not None
                else None
            )
            require_pinned_cpu_selectable(
                profile, resource.capability_view if resource is not None else None
            )
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(str(system_id), exc)
    envelope = await _admit_reprovision(conn, ctx, system, profile, digest, dedup_key)
    if idempotency_key is not None:
        await record_envelope(
            conn,
            principal=ctx.principal,
            key=idempotency_key,
            project=system.project,
            kind=idempotency_kind,
            envelope=envelope,
        )
    return envelope


async def _audit_destructive_denied(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    op_kind: JobKind,
    missing: list[str],
) -> None:
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=f"systems.{op_kind.value}",
            object_kind="systems",
            object_id=system.id,
            transition=f"{op_kind.value}:denied",
            args={"system_id": str(system.id), "missing": missing},
            project=system.project,
        ),
    )


async def _has_live_run(conn: AsyncConnection, system_id: UUID) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM runs WHERE system_id = %s AND state = ANY(%s) LIMIT 1",
            (system_id, [s.value for s in _NON_TERMINAL_RUN]),
        )
        return await cur.fetchone() is not None


async def _job_for_dedup_key(conn: AsyncConnection, dedup_key: str) -> Job | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM jobs WHERE dedup_key = %s", (dedup_key,))
        row = await cur.fetchone()
    return validate_stored(Job, row) if row else None


async def _settling_reprovision_job(conn: AsyncConnection, dedup_key: str) -> UUID | None:
    async with conn.cursor() as cur:
        await cur.execute(
            _REPROVISION_SETTLING_SQL,
            (
                dedup_key,
                [s.value for s in _LIVE_JOB_STATES],
                JobState.CANCELED.value,
                ErrorCategory.LEASE_EXPIRED.value,
                _REPROVISION_SETTLE,
            ),
        )
        row = await cur.fetchone()
    return None if row is None else row[0]


async def _admit_reprovision(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    profile: ProvisioningProfile,
    digest: str,
    dedup_key: str,
) -> ToolResponse:
    """Transition ready->reprovisioning, write the new profile, enqueue the keyed job."""
    await SYSTEMS.update_state(conn, system.id, SystemState.REPROVISIONING)
    await conn.execute(
        "UPDATE systems SET provisioning_profile = %s WHERE id = %s",
        (Jsonb(dump_profile(profile)), system.id),
    )
    # Clear a LOCAL System's prior resolved_cpu: the rebuilt domain has not booted, so the
    # live-verified value is unknown until Phase C re-reads it at the reprovision READY boundary
    # (ADR-0369). Remote/fault keep their mint-time snapshot — reprovision does not re-mint it and
    # the bound host is unchanged, so NULLing it would drop the signal permanently.
    if profile.provider.kind is ResourceKind.LOCAL_LIBVIRT:
        await conn.execute("UPDATE systems SET resolved_cpu = NULL WHERE id = %s", (system.id,))
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="systems.reprovision",
            object_kind="systems",
            object_id=system.id,
            transition="ready->reprovisioning",
            args={"system_id": str(system.id), "profile_digest": digest},
            project=system.project,
        ),
    )
    job = await queue.enqueue(
        conn,
        JobKind.REPROVISION,
        ReprovisionPayload(system_id=str(system.id), profile_digest=digest),
        job_authorizing(ctx, system.project),
        dedup_key,
        # The caller refused a row that may still hide a handler, so any row left is settled.
        recycle=queue.JobRecyclePolicy.TERMINAL_OR_CANCELED,
    )
    return job_envelope(job, "system_id", system.id)


async def teardown_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    *,
    idempotency_key: str | None = None,
    resolver: ProviderResolver | None = None,
) -> ToolResponse:
    """Enqueue an idempotent teardown for a System the caller's project administers.

    Requires ``admin`` on the owning project (ADR-0129). Teardown is the normal lifecycle
    terminus of a granted System, so it does not run the destructive-op gate — the gate's role
    and profile-opt-in factors add no safety for destroying your own System. ``RoleDenied`` is
    caught locally (not propagated to ``DenialAuditMiddleware``),
    so the denial is audited once, keyed on ``system_id``, with ``data["missing_checks"]``. The
    admin check runs before the idempotent ``torn_down`` short-circuit, so a non-admin never
    learns a System's terminal state.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            if idempotency_key is not None:
                try:
                    validate_idempotency_key(idempotency_key)
                except CategorizedError as exc:
                    return ToolResponse.failure_from_error("idempotency_key", exc)
            try:
                return await _teardown_locked(conn, ctx, uid, system_id, idempotency_key, resolver)
            except UniqueViolation:
                if idempotency_key is None:
                    raise  # only the keyed path records, so an unkeyed collision is not ours
                try:
                    return await resolve_conflict(
                        conn,
                        principal=ctx.principal,
                        key=idempotency_key,
                        kind=object_scoped_kind(_TEARDOWN_KIND, uid),
                    )
                except CategorizedError as exc:
                    return ToolResponse.failure_from_error("idempotency_key", exc)


async def _teardown_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    system_id: str,
    idempotency_key: str | None,
    resolver: ProviderResolver | None,
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, uid):
        system = await SYSTEMS.get(conn, uid)
        if system is None or system.project not in ctx.projects:
            return _config_error(system_id)
        allocation = await ALLOCATIONS.get(conn, system.allocation_id)
        if allocation is None or allocation.project not in ctx.projects:
            return _config_error(system_id)
        try:
            require_role(ctx, allocation.project, Role.ADMIN)
        except RoleDenied:
            await _audit_destructive_denied(conn, ctx, system, _TEARDOWN, ["admin_role"])
            return _authz_denied(system_id, ["admin_role"])

        async def record_routed_job(
            connection: AsyncConnection, job: Job, skip_matching_key: bool
        ) -> None:
            if idempotency_key is None:
                return
            envelope = job_envelope(job, "system_id", system.id)
            if skip_matching_key and await _key_names_job(
                connection, ctx, system.id, idempotency_key, envelope.object_id
            ):
                return
            await record_envelope(
                connection,
                principal=ctx.principal,
                key=idempotency_key,
                project=system.project,
                kind=object_scoped_kind(_TEARDOWN_KIND, system.id),
                envelope=envelope,
            )

        try:
            routed = await route_external_boot_teardown(
                conn,
                system,
                job_authorizing(ctx, system.project),
                resolver,
                on_recycle=partial(_audit_teardown_recycle, conn, ctx, system),
                recorder=record_routed_job if idempotency_key is not None else None,
            )
        except TeardownRoutingDenied as exc:
            return teardown_routing_failure(system_id, exc.error, ctx)
        if routed is not None:
            return job_envelope(routed, "system_id", system.id)
        if idempotency_key is not None:
            replay = await resolve_envelope_replay(
                conn,
                principal=ctx.principal,
                key=idempotency_key,
                kind=object_scoped_kind(_TEARDOWN_KIND, uid),
            )
            if replay is not None:
                return replay
        if system.state is SystemState.TORN_DOWN:
            # The System is already terminal, but its Allocation may still be `active`; point the
            # agent at the second wind-down step so the idempotent replay steers identically to a
            # freshly-completed teardown job (#1385, ADR-0414). This branch is reached only after
            # the ADMIN gate above, so the caller always holds the CONTRIBUTOR that
            # allocations.release requires — no RBAC filtering needed.
            return ToolResponse.success(
                system_id,
                "torn_down",
                suggested_next_actions=["allocations.release", "systems.get"],
                data={"project": system.project},
            )
        if system.state is SystemState.REPROVISIONING:
            # The one non-terminal state with no teardown edge (#2928, ADR-0435). Refuse ahead of
            # the dedup replay and the enqueue: a live `{uid}:teardown` row is not replayed and a
            # failed one is not recycled into a job the handler would refuse again. Re-running
            # once the reprovision settles recycles a failed row.
            return ToolResponse.failure(
                system_id,
                ErrorCategory.CONFLICT,
                detail="System is mid-reprovision; retry systems.teardown once it settles",
                suggested_next_actions=["systems.get"],
                data={"current_status": system.state.value},
            )
        authority_binding = await authority_system_binding(conn, uid)
        if authority_binding is not None:
            return await _enqueue_preactivation_authority_teardown(
                conn,
                ctx,
                system,
                authority_binding,
                system_id,
                idempotency_key,
            )
        # `{uid}:teardown` is stable, so an unkeyed repeat replays a live, succeeded, or canceled
        # teardown job. A dead-lettered `failed` one is reset to a fresh attempt so provider and
        # core reclaim run again (#2929, ADR-0435). Both replay paths stay below the
        # current-activation safety fence: an old ordinary teardown job cannot gain authority from
        # its replay envelope.
        replay = await dedup_replay(
            conn, _teardown_dedup_key(uid), recycle=queue.JobRecyclePolicy.FAILED
        )
        if replay is not None:
            return job_envelope(replay, "system_id", uid)
        try:
            await check_external_boot_admission(
                conn, system.id, ExternalBootOperation.SYSTEM_TEARDOWN, project=system.project
            )
        except ExternalBootDenied as exc:
            return teardown_routing_failure(system_id, exc, ctx)
        prior = await queue.get_by_dedup_key(conn, _teardown_dedup_key(uid))
        job, changed = await queue.enqueue_with_status(
            conn,
            JobKind.TEARDOWN,
            TeardownPayload(system_id=str(uid)),
            job_authorizing(ctx, system.project),
            _teardown_dedup_key(uid),
            recycle=queue.JobRecyclePolicy.FAILED,
        )
        # A recycle keeps the first enqueuer as `job.authorizing` (ADR-0447); audit the requester.
        if prior is not None and changed:
            await _audit_teardown_recycle(conn, ctx, system, job)
        envelope = job_envelope(job, "system_id", uid)
        if idempotency_key is not None:
            await record_envelope(
                conn,
                principal=ctx.principal,
                key=idempotency_key,
                project=system.project,
                kind=object_scoped_kind(_TEARDOWN_KIND, uid),
                envelope=envelope,
            )
        return envelope


async def _enqueue_preactivation_authority_teardown(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    binding: AuthoritySystemBinding,
    system_id: str,
    idempotency_key: str | None,
) -> ToolResponse:
    """Enqueue and bind one activation-free teardown to immutable ownership."""
    try:
        # `job.authorizing` stays the first enqueuer's by design (ADR-0447); audit the requester.
        # A failed job, or one whose final attempt's lease lapsed, restarts unless an absence
        # receipt is pending (ADR-0620, #3617, #3627).
        job = await enqueue_preactivation_teardown(
            conn,
            system,
            binding,
            job_authorizing(ctx, system.project),
            recycle=queue.JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED,
            on_recycle=partial(_audit_teardown_recycle, conn, ctx, system),
        )
    except CategorizedError as exc:
        if exc.category is not ErrorCategory.CONFLICT:
            raise
        return ToolResponse.failure(
            system_id,
            ErrorCategory.CONFLICT,
            detail="an ordinary teardown job cannot be replayed for an authority-owned System",
            suggested_next_actions=["jobs.wait", "systems.get"],
            data={"reason": "ordinary_teardown_fenced_by_system_authority"},
        )
    envelope = job_envelope(job, "system_id", system.id)
    if idempotency_key is not None:
        await record_envelope(
            conn,
            principal=ctx.principal,
            key=idempotency_key,
            project=system.project,
            kind=object_scoped_kind(_TEARDOWN_KIND, system.id),
            envelope=envelope,
        )
    return envelope


async def _audit_teardown_recycle(
    conn: AsyncConnection, ctx: RequestContext, system: System, job: Job
) -> None:
    """Record who asked for a teardown recycle, in the recycle's transaction.

    A recycled job keeps the ``authorizing`` principal of whoever first enqueued it (ADR-0447, with
    ADR-0435 and ADR-0620), so the requester would otherwise leave no trace. The recycling caller
    audits its own invocation; the row is written only when the job was actually restarted.
    """
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="systems.teardown",
            object_kind="systems",
            object_id=system.id,
            transition="teardown:recycled",
            args={"system_id": str(system.id), "job_id": str(job.id)},
            project=system.project,
        ),
    )


async def _key_names_job(
    conn: AsyncConnection, ctx: RequestContext, system_id: UUID, key: str, job_id: str | None
) -> bool:
    stored = await resolve_envelope_replay(
        conn, principal=ctx.principal, key=key, kind=object_scoped_kind(_TEARDOWN_KIND, system_id)
    )
    return stored is not None and stored.object_id == job_id
