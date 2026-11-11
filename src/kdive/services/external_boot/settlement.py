"""The `platform_admin` entry to stranded activation settlement (ADR-0816)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.external_boot_activations import (
    ExternalBootActivationRepository,
    SettlementStatus,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, ObserveStrandedActivationPayload
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, require_platform_role
from kdive.services.external_boot.admission import (
    ExternalBootOperation,
    check_external_boot_admission,
)
from kdive.services.systems.authority_owned import authority_system_binding

_REPOSITORY = ExternalBootActivationRepository()
SETTLEMENT_REASON_MAX_BYTES = 1024


@dataclass(frozen=True, slots=True)
class StrandedObservationOutcome:
    """The observation job's standing, the one read of a platform-internal job (ADR-0816)."""

    job_id: UUID
    state: JobState
    reason: str | None
    message: str | None


async def settled_at(conn: AsyncConnection, activation_id: UUID) -> datetime | None:
    """When ``activation_id`` was settled, or ``None``: the key every teardown fence reads."""
    cursor = await conn.execute(
        "SELECT settled_at FROM external_boot_activation_settlements "
        "WHERE activation_id = %s AND settled_at IS NOT NULL",
        (activation_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else row[0]


async def settled_system_awaiting_teardown(
    conn: AsyncConnection, allocation_id: UUID
) -> UUID | None:
    """A System of ``allocation_id`` with a settled activation that is not yet torn down."""
    cursor = await conn.execute(
        "SELECT s.id FROM systems s WHERE s.allocation_id = %s AND s.state <> 'torn_down' "
        "  AND EXISTS (SELECT 1 FROM external_boot_activation_settlements st "
        "              WHERE st.system_id = s.id AND st.settled_at IS NOT NULL) "
        "ORDER BY s.id LIMIT 1",
        (allocation_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else row[0]


def settlement_reason(reason: str) -> str | None:
    """The trimmed reason when it fits the settlement function's bound, else ``None``."""
    reason = reason.strip()
    if "\0" in reason or not 1 <= len(reason.encode()) <= SETTLEMENT_REASON_MAX_BYTES:
        return None
    return reason


def _dedup_key(activation_id: UUID) -> str:
    return f"stranded-observation:{activation_id}"


def _observation_refused(
    activation_id: UUID, reason: str, category: ErrorCategory = ErrorCategory.CONFLICT
) -> CategorizedError:
    return CategorizedError(
        f"stranded activation {activation_id} cannot be observed: {reason}",
        category=category,
        details={"reason": reason},
        terminal=True,
    )


async def request_stranded_observation(
    conn: AsyncConnection, ctx: RequestContext, *, activation_id: UUID
) -> Job:
    """Enqueue the job that produces both owners' settlement evidence (ADR-0816).

    A caller lacking `platform_admin` gets the bare ``AuthorizationError``, as in
    :func:`settle_stranded_activation`. The caller reads the outcome through
    :func:`read_stranded_observation`, because job tools answer `not_found` for this kind.
    """
    require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    activation = await _REPOSITORY.get(conn, activation_id)
    if activation is None:
        raise _observation_refused(activation_id, "not_found", ErrorCategory.NOT_FOUND)
    system_id = activation.system_id
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise _observation_refused(activation_id, "not_found", ErrorCategory.NOT_FOUND)
        if await authority_system_binding(conn, system_id) is not None:
            raise _observation_refused(activation_id, "authority_owned")
        activation = await _REPOSITORY.get(conn, activation_id)
        if activation is None or activation.state is not ExternalBootActivationState.PREPARING:
            raise _observation_refused(activation_id, "not_preparing")
        await check_external_boot_admission(
            conn,
            system_id,
            ExternalBootOperation.OBSERVE_STRANDED_ACTIVATION,
            project=system.project,
        )
        return await queue.enqueue(
            conn,
            JobKind.OBSERVE_STRANDED_ACTIVATION,
            ObserveStrandedActivationPayload(
                schema="observe-stranded-activation-v1",
                system_id=str(system_id),
                activation_id=str(activation_id),
            ),
            Authorizing(
                principal=ctx.principal, agent_session=ctx.agent_session, project=system.project
            ),
            _dedup_key(activation_id),
            recycle=queue.JobRecyclePolicy.TERMINAL,
        )


async def read_stranded_observation(
    conn: AsyncConnection, ctx: RequestContext, *, activation_id: UUID
) -> StrandedObservationOutcome | None:
    """Return the observation's standing, or `None` when none was requested."""
    require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT id, state, failure_context FROM jobs WHERE dedup_key = %s",
            (_dedup_key(activation_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    context = row["failure_context"]
    return StrandedObservationOutcome(
        job_id=row["id"],
        state=JobState(row["state"]),
        reason=context.get("failure_detail_reason"),
        message=context.get("failure_message"),
    )


async def settle_stranded_activation(
    conn: AsyncConnection, ctx: RequestContext, *, activation_id: UUID, actor: str, reason: str
) -> SettlementStatus:
    """Settle one stranded activation, or raise naming the closed refusal reason.

    A caller lacking `platform_admin` gets the bare ``AuthorizationError``: the tool handler
    calls ``audit_platform_denial`` and returns ``ToolResponse.denied``, as the break-glass tools
    do. The settlement function writes only the success audit row, in its own transaction, so the
    tool handler also audits each refusal raised here (ADR-0816).
    """
    require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    normalized = settlement_reason(reason)
    if normalized is None:
        raise CategorizedError(
            "settling a stranded activation requires a reason of 1 to "
            f"{SETTLEMENT_REASON_MAX_BYTES} bytes",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "reason_required"},
        )
    status = await _REPOSITORY.settle_stranded(
        conn,
        activation_id=activation_id,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        actor=actor,
        reason=normalized,
    )
    if status in {SettlementStatus.APPLIED, SettlementStatus.REPLAYED}:
        return status
    raise CategorizedError(
        f"activation {activation_id} was not settled: {status.value}",
        category=(
            ErrorCategory.NOT_FOUND
            if status is SettlementStatus.NOT_FOUND
            else ErrorCategory.CONFLICT
        ),
        details={"reason": status.value},
        terminal=True,
    )


def _settle_refused(
    activation_id: UUID,
    reason: str,
    detail: str,
    category: ErrorCategory = ErrorCategory.CONFLICT,
    job_id: UUID | None = None,
) -> CategorizedError:
    details: dict[str, object] = {"reason": reason}
    if job_id is not None:
        details["job_id"] = str(job_id)
    return CategorizedError(
        f"stranded activation {activation_id} was not settled: {detail}",
        category=category,
        details=details,
        terminal=True,
    )


def _require_succeeded(activation_id: UUID, outcome: StrandedObservationOutcome | None) -> None:
    """Refuse unless the latest observation succeeded, with the job's own reason when it failed."""
    if outcome is None:
        raise _settle_refused(activation_id, "observation_missing", "no observation was requested")
    if outcome.state is JobState.SUCCEEDED:
        return
    if outcome.state in {JobState.QUEUED, JobState.RUNNING}:
        raise _settle_refused(
            activation_id,
            "observation_pending",
            "the observation is still running",
            job_id=outcome.job_id,
        )
    raise _settle_refused(
        activation_id,
        outcome.reason or f"observation_{outcome.state.value}",
        f"the observation {outcome.state.value}: {outcome.message or 'no reason recorded'}",
        job_id=outcome.job_id,
    )


async def settle_observed_stranded_activation(
    conn: AsyncConnection, ctx: RequestContext, *, activation_id: UUID, actor: str, reason: str
) -> UUID:
    """Settle on a succeeded latest observation, under the System lock; return the System id.

    A failed rerun leaves the earlier records standing for their hour, so settling on anything but
    a succeeded latest observation would use evidence a refusal contradicted (ADR-0816, #3574).
    The lock is the one :func:`request_stranded_observation` takes, so no observation is recycled
    between the read and the settlement.
    """
    require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    activation = await _REPOSITORY.get(conn, activation_id)
    if activation is None:
        raise _settle_refused(
            activation_id, "not_found", "no such activation", ErrorCategory.NOT_FOUND
        )
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, activation.system_id),
    ):
        outcome = await read_stranded_observation(conn, ctx, activation_id=activation_id)
        _require_succeeded(activation_id, outcome)
        await settle_stranded_activation(
            conn, ctx, activation_id=activation_id, actor=actor, reason=reason
        )
    return activation.system_id
