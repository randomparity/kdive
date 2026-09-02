"""The three external-boot recovery contracts (ADR-0583, #2117).

Each service resolves its object, authorizes the caller, decides admission against the
System-wide matrix, and then reports that the external-boot recovery executor is not
installed. None of them writes, so every response is a failure envelope.

Why none of them writes: no production caller drives ``ExternalBootActivationRepository``'s
transition methods on this branch, ``allocate_external_boot_authority`` (migration 0122) is
gated on ``kdive_worker`` membership and revoked from the ``kdive_server`` role the MCP
server runs as, and ``ExternalBootAuthorityMarkerV1`` requires a ``provider_kind`` and
``authority_instance`` that neither an activation nor a reservation row carries. A tool that
began a recovery attempt here could not finish it, so these report the missing executor
instead and #2118 promotes them with it.

Ordering is resolve, authorize, admit, report. Authorization runs before the admission read
so an unauthorized caller learns nothing about whether the System carries an activation.
"""

from __future__ import annotations

from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter, ValidationError

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.external_boot_activation import Digest
from kdive.domain.lifecycle.records import Run
from kdive.log import bind_context
from kdive.mcp.platform_auth import audit_platform_denial
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    require_platform_role,
    require_role,
)
from kdive.serialization import JsonValue
from kdive.services.debug.sessions import active_session_ids_for_system
from kdive.services.external_boot import (
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)

_UNAVAILABLE_REASON = "recovery_executor_unavailable"

RELEASE_TOOL = "runs.release_external_boot"
RESOLVE_CONFLICT_TOOL = "systems.resolve_external_boot_conflict"
ORPHAN_TOOL = "ops.resolve_recovery_orphan"

#: The single resolution ADR-0583 defines: put the recorded source state back.
SUPPORTED_RESOLUTION_OPERATION = "restore-recorded-source"
SUPPORTED_DISPOSITIONS = frozenset({"delete", "adopt"})
MAX_OBJECT_IDENTITIES = 64
#: Per-identity character cap, matching the reservation model's ``store_identity`` bound.
MAX_IDENTITY_LENGTH = 1024

_ACTIVE_JOB_STATES = [JobState.QUEUED.value, JobState.RUNNING.value]

# System-scoped kinds carry the System in their payload; run-scoped kinds carry only a
# `run_id`, so the Run join is what makes the refusal cover another Run's in-flight work.
_ACTIVE_JOBS_SQL: LiteralString = (
    "SELECT j.id FROM jobs j LEFT JOIN runs r ON r.id::text = j.payload->>'run_id' "
    "WHERE j.state = ANY(%s) AND (j.payload->>'system_id' = %s OR r.system_id = %s) "
    "ORDER BY j.id"
)

_REPOSITORY = ExternalBootActivationRepository()
_IDENTITY = TypeAdapter(Digest)


def _executor_unavailable(object_id: str, tool: str) -> ToolResponse:
    """The one terminal response all three contracts share.

    One reason string for all three because one thing is missing: the external-boot recovery
    executor #2118 owns. Built here rather than at each call site so the reason and the
    disclosure cannot drift apart.
    """
    return ToolResponse.failure(
        object_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=(
            f"{tool} accepted this request but cannot serve it: the external-boot recovery "
            "executor is not installed, so nothing was changed"
        ),
        suggested_next_actions=["systems.get"],
        data={"reason": _UNAVAILABLE_REASON},
    )


def _config_error(object_id: str, *, reason: str, detail: str, next_action: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=detail,
        suggested_next_actions=[next_action],
        data={"reason": reason},
    )


def _conflict(
    object_id: str,
    *,
    reason: str,
    detail: str,
    next_actions: list[str],
    data: dict[str, JsonValue] | None = None,
) -> ToolResponse:
    payload: dict[str, JsonValue] = {"reason": reason}
    payload.update(data or {})
    return ToolResponse.failure(
        object_id,
        ErrorCategory.CONFLICT,
        detail=detail,
        suggested_next_actions=next_actions,
        data=payload,
    )


def _unresolved_run(run_id: str) -> ToolResponse:
    """One envelope for a missing Run and for a Run in a project the caller does not hold."""
    return _config_error(
        run_id,
        reason="unresolved_run",
        detail="run_id does not resolve to a Run available to this caller",
        next_action="runs.get",
    )


def _unresolved_system(system_id: str) -> ToolResponse:
    """One envelope for a missing System and for one the caller may not see."""
    return _config_error(
        system_id,
        reason="unresolved_system",
        detail="system_id does not resolve to a System available to this caller",
        next_action="systems.get",
    )


def _denial(object_id: str, exc: ExternalBootDenied) -> ToolResponse:
    return ToolResponse.failure_from_error(object_id, exc, suggested_next_actions=exc.next_actions)


async def _active_job_ids_for_system(conn: AsyncConnection, system_id: UUID) -> list[str]:
    """Every queued or running job for the System, whichever Run owns it."""
    async with conn.cursor() as cur:
        await cur.execute(_ACTIVE_JOBS_SQL, (_ACTIVE_JOB_STATES, str(system_id), system_id))
        rows = await cur.fetchall()
    return [str(row[0]) for row in rows]


async def request_release(
    pool: AsyncConnectionPool, ctx: RequestContext, *, run_id: str
) -> ToolResponse:
    """Admit a release of the Run's external-boot activation, then report the missing executor.

    Resolves and authorizes the Run, then decides ``external_boot_release`` against the
    activation restricting its System and refuses the two conditions ADR-0583 names as
    blocking a release. The caller gets the same refusal it will get once the executor lands;
    an admissible request gets ``configuration_error`` with
    ``reason=recovery_executor_unavailable`` and no activation row is touched.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _unresolved_run(run_id)
            if run.system_id is None:
                return _config_error(
                    run_id,
                    reason="run_not_bound",
                    detail="this Run is bound to no System, so it holds no external boot",
                    next_action="runs.get",
                )
            require_role(ctx, run.project, Role.CONTRIBUTOR)
            return await _release_locked(conn, run, run.system_id)


async def _release_locked(conn: AsyncConnection, run: Run, system_id: UUID) -> ToolResponse:
    """Decide the release under the System lock, so every read sees one consistent activation.

    ``conn`` has already read the Run, so this transaction is a SAVEPOINT and the lock releases
    at end-of-request; only the envelope render follows the block, and nothing is written.

    The restricting activation is read directly before the guard because the guard cannot
    express "nothing to release": it returns ``None`` both for an admitted operation and for a
    System no activation restricts — it must, since every ordinary call site needs an absent
    activation to admit its work — and its denial details are exactly ``activation_id``,
    ``activation_state``, and ``owning_run_id``, none of which exist when there is no row.
    """
    object_id = str(run.id)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        if await _REPOSITORY.get_restricting_for_system(conn, system_id) is None:
            return _conflict(
                object_id,
                reason="no_active_activation",
                detail="no external-boot activation restricts this Run's System",
                next_actions=["runs.get"],
            )
        try:
            await check_external_boot_admission(
                conn, system_id, ExternalBootOperation.EXTERNAL_BOOT_RELEASE, run_id=run.id
            )
        except ExternalBootDenied as exc:
            return _denial(object_id, exc)
        job_ids = await _active_job_ids_for_system(conn, system_id)
        if job_ids:
            return _conflict(
                object_id,
                reason="system_job_active",
                detail="a queued or running job holds this System; release once it settles",
                next_actions=["jobs.wait", "runs.get"],
                data={"job_ids": list(job_ids)},
            )
        session_ids = await active_session_ids_for_system(conn, system_id)
        if session_ids:
            return _conflict(
                object_id,
                reason="debug_session_active",
                detail="a debug session is attaching to or live on this System",
                next_actions=["debug.detach", "runs.get"],
                data={"session_ids": list(session_ids)},
            )
    return _executor_unavailable(object_id, RELEASE_TOOL)


def _resolution_input_error(
    system_id: str, operation: str, observed_identity: str
) -> ToolResponse | None:
    if operation != SUPPORTED_RESOLUTION_OPERATION:
        return _config_error(
            system_id,
            reason="unsupported_resolution_operation",
            detail=(
                "operation must be exactly "
                f"{SUPPORTED_RESOLUTION_OPERATION!r}, the one resolution ADR-0583 defines"
            ),
            next_action="systems.get",
        )
    if not _is_identity_shaped(observed_identity):
        return _config_error(
            system_id,
            reason="invalid_observed_identity",
            detail="observed_identity must be a 'sha256:<64 lowercase hex>' composite state",
            next_action="systems.get",
        )
    return None


def _is_identity_shaped(value: str) -> bool:
    """Bound the caller's identity, then check its shape against the stored digest form.

    Shape only. The value is never compared with the activation's recorded composite state:
    that compare-and-set is one half of ``begin_recovery_attempt``, and running it would commit
    the transition this module cannot finish. The length check precedes the pattern check so an
    oversized value is refused before any matching work.
    """
    if len(value) > MAX_IDENTITY_LENGTH:
        return False
    try:
        _IDENTITY.validate_python(value)
    except ValidationError:
        return False
    return True


async def resolve_conflict(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    operation: str,
    observed_identity: str,
) -> ToolResponse:
    """Admit a recovery-conflict resolution, then report the missing executor.

    Resolves and authorizes the System, validates ``operation`` and the shape of
    ``observed_identity``, and decides ``external_boot_resolve_conflict`` — which the matrix
    admits only in ``recovery_conflict``. No job or session refusal applies: a System in
    ``recovery_conflict`` already fails the matrix for every operation that could start one.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _unresolved_system(system_id)
            require_role(ctx, system.project, Role.ADMIN)
            invalid = _resolution_input_error(system_id, operation, observed_identity)
            if invalid is not None:
                return invalid
            return await _resolve_conflict_locked(conn, uid)


async def _resolve_conflict_locked(conn: AsyncConnection, system_id: UUID) -> ToolResponse:
    """Decide the resolution under the System lock.

    See :func:`_release_locked` for why the restricting activation is read directly rather
    than inferred from the guard.
    """
    object_id = str(system_id)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        if await _REPOSITORY.get_restricting_for_system(conn, system_id) is None:
            return _conflict(
                object_id,
                reason="no_recovery_conflict",
                detail="no external-boot activation restricts this System, so none is conflicted",
                next_actions=["runs.get"],
            )
        try:
            await check_external_boot_admission(
                conn, system_id, ExternalBootOperation.EXTERNAL_BOOT_RESOLVE_CONFLICT
            )
        except ExternalBootDenied as exc:
            return _denial(object_id, exc)
    return _executor_unavailable(object_id, RESOLVE_CONFLICT_TOOL)


def _orphan_input_error(
    system_id: str, object_identities: list[str], disposition: str
) -> ToolResponse | None:
    if disposition not in SUPPORTED_DISPOSITIONS:
        return _config_error(
            system_id,
            reason="unsupported_disposition",
            detail=f"disposition must be one of {', '.join(sorted(SUPPORTED_DISPOSITIONS))}",
            next_action="systems.get",
        )
    within_bounds = 0 < len(object_identities) <= MAX_OBJECT_IDENTITIES and all(
        0 < len(identity) <= MAX_IDENTITY_LENGTH for identity in object_identities
    )
    if not within_bounds:
        return _config_error(
            system_id,
            reason="invalid_object_identities",
            detail=(
                f"object_identities must hold 1 to {MAX_OBJECT_IDENTITIES} references, each "
                f"1 to {MAX_IDENTITY_LENGTH} characters"
            ),
            next_action="systems.get",
        )
    return None


async def resolve_recovery_orphan(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    object_identities: list[str],
    disposition: str,
) -> ToolResponse:
    """Admit a quarantined recovery-object repair, then report the missing executor.

    The platform role is enforced before the System is *resolved*, matching the break-glass
    ``ops`` tools this one registers beside: a caller without ``platform_admin`` learns nothing
    about which System ids exist. Only the id's syntax is checked first, so the denial audit
    below records a bounded identifier rather than arbitrary caller input. It runs no admission
    check — ADR-0583 scopes the repair to quarantined recovery objects, which are not the
    activation the matrix keys on, and no quarantine record exists to read yet.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=ORPHAN_TOOL, scope=f"denied:{uid}", args={"system_id": str(uid)}
        )
        return ToolResponse.denied(system_id, missing_roles=[PlatformRole.PLATFORM_ADMIN])
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
        if system is None:
            return _unresolved_system(system_id)
        invalid = _orphan_input_error(system_id, object_identities, disposition)
        if invalid is not None:
            return invalid
        return _executor_unavailable(system_id, ORPHAN_TOOL)


__all__ = [
    "MAX_IDENTITY_LENGTH",
    "MAX_OBJECT_IDENTITIES",
    "ORPHAN_TOOL",
    "RELEASE_TOOL",
    "RESOLVE_CONFLICT_TOOL",
    "SUPPORTED_DISPOSITIONS",
    "SUPPORTED_RESOLUTION_OPERATION",
    "request_release",
    "resolve_conflict",
    "resolve_recovery_orphan",
]
