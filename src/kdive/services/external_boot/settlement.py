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
_REASON_MAX_BYTES = 1024


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
