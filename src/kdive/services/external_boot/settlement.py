"""The `platform_admin` entry to stranded activation settlement (ADR-0816)."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.external_boot_activations import (
    ExternalBootActivationRepository,
    SettlementStatus,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, require_platform_role

_REPOSITORY = ExternalBootActivationRepository()


async def settle_stranded_activation(
    conn: AsyncConnection, ctx: RequestContext, *, activation_id: UUID, actor: str, reason: str
) -> SettlementStatus:
    """Settle one stranded activation, or raise naming the closed refusal reason.

    The caller owns denial auditing, as the break-glass tools do; the settlement function writes
    the success audit row in its own transaction.
    """
    require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    if not reason.strip():
        raise CategorizedError(
            "settling a stranded activation requires a non-blank reason",
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
