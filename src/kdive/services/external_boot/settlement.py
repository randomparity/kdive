"""The `platform_admin` entry to stranded activation settlement (ADR-0816)."""

from __future__ import annotations

from dataclasses import dataclass
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
_REASON_MAX_BYTES = 1024


@dataclass(frozen=True, slots=True)
class StrandedObservationOutcome:
    """The observation job's standing, the one read of a platform-internal job (ADR-0816)."""

    job_id: UUID
    state: JobState
    reason: str | None
    message: str | None


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
    reason = reason.strip()
    if "\0" in reason or not 1 <= len(reason.encode()) <= _REASON_MAX_BYTES:
        raise CategorizedError(
            f"settling a stranded activation requires a reason of 1 to {_REASON_MAX_BYTES} bytes",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "reason_required"},
        )
    status = await _REPOSITORY.settle_stranded(
        conn,
        activation_id=activation_id,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        actor=actor,
        reason=reason,
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
