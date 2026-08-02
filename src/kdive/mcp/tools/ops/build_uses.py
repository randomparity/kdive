"""Operator recovery of reusable-build pins held by proven-dead worker incarnations."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import MAX_LIST_LIMIT, clamp_list_limit
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.services.runs.build_use import recover_build_use_in_transaction

_TOOL = "ops.recover_build_use"
_LIST_TOOL = "ops.build_uses_list"
_MAX_HOLDER_CHARS = 512
_MAX_REASON_CHARS = 512


def _failure(use_id: UUID, message: str) -> ToolResponse:
    return ToolResponse.failure_from_error(
        str(use_id),
        CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR),
        suggested_next_actions=[_TOOL],
    )


async def _audit_refusal(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    use_id: UUID,
    holder: str,
    reason: str,
    outcome: str,
) -> None:
    """Durably record a bounded server-derived refusal before returning it."""
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_TOOL,
                scope=f"build-use-recovery:{outcome}",
                args={"use_id": use_id, "holder": holder, "reason": reason},
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


async def list_build_uses(
    pool: AsyncConnectionPool, ctx: RequestContext, *, limit: int = 50
) -> ToolResponse:
    """List a bounded oldest-first set of persistent pins for operator diagnosis."""
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
    except AuthorizationError:
        await audit_platform_denial(pool, ctx, tool=_LIST_TOOL, scope="build-use-recovery")
        return ToolResponse.denied("build-uses", missing_roles=[PlatformRole.PLATFORM_OPERATOR])
    capped = clamp_list_limit(limit)
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT use_id, investigation_id, generation, job_id, attempt, "
                "holder_worker_id, created_at FROM investigation_build_uses "
                "ORDER BY created_at, use_id LIMIT %s",
                (capped,),
            )
        ).fetchall()
        async with conn.transaction():
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=_LIST_TOOL,
                    scope="build-use-recovery",
                    args={"limit": capped},
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
    items = [
        ToolResponse.success(
            str(row[0]),
            "pinned",
            data={
                "investigation_id": str(row[1]),
                "generation": str(row[2]),
                "job_id": str(row[3]),
                "attempt": str(row[4]),
                "holder": row[5],
                "created_at": row[6].isoformat(),
            },
        )
        for row in rows
    ]
    return ToolResponse.collection(
        "build-uses",
        "ok",
        items,
        suggested_next_actions=[_TOOL],
        data={"limit": capped},
    )


async def recover_build_use(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    use_id: UUID,
    holder: str,
    reason: str,
) -> ToolResponse:
    """Recover one exact pin after operator auth and independent incarnation-death proof."""
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
    except AuthorizationError:
        await audit_platform_denial(pool, ctx, tool=_TOOL, scope="build-use-recovery")
        return ToolResponse.denied(str(use_id), missing_roles=[PlatformRole.PLATFORM_OPERATOR])
    clean_reason = reason.strip()
    if not clean_reason or len(clean_reason) > _MAX_REASON_CHARS or len(holder) > _MAX_HOLDER_CHARS:
        await _audit_refusal(
            pool,
            ctx,
            use_id=use_id,
            holder=holder,
            reason=clean_reason,
            outcome="invalid_input",
        )
        return _failure(use_id, "holder and reason must be non-empty and at most 512 characters")
    async with pool.connection() as conn, conn.transaction():
        recovered = await recover_build_use_in_transaction(
            conn,
            use_id,
            confirmed_worker_id=holder,
            recovered_by=ctx.principal,
            evidence="durable worker-incarnation termination record",
            reason=clean_reason,
        )
        if not recovered:
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=_TOOL,
                    scope="build-use-recovery:use_or_holder_mismatch",
                    args={"use_id": use_id, "holder": holder, "reason": clean_reason},
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
            return _failure(
                use_id,
                "build-use pin was absent, its exact holder did not match, or durable "
                "termination evidence was not recorded",
            )
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_TOOL,
                scope="build-use-recovery",
                args={"use_id": use_id, "holder": holder, "reason": clean_reason},
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )
    return ToolResponse.success(str(use_id), "recovered", data={"holder": holder})


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the authenticated build-use recovery tool."""

    @app.tool(name=_LIST_TOOL, annotations=_docmeta.read_only(), meta={"maturity": "implemented"})
    async def ops_build_uses_list(
        limit: Annotated[
            int,
            Field(
                description=(
                    f"Maximum oldest-first pin rows returned; server-capped at {MAX_LIST_LIMIT}."
                )
            ),
        ] = 50,
    ) -> ToolResponse:
        """List persistent reusable-build pins. Requires platform operator.

        Conditionally available only when durable worker-termination witnesses are configured.
        A stale job lease is diagnostic context only, never proof that its holder stopped. Pass an
        exact returned use id and holder to `ops.recover_build_use` only after operator review.
        """
        return await list_build_uses(pool, current_context(), limit=limit)

    @app.tool(name=_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def ops_recover_build_use(
        use_id: Annotated[UUID, Field(description="Exact stranded build-use UUID.")],
        holder: Annotated[
            str,
            Field(
                description=(
                    "Exact worker incarnation recorded on that use row; "
                    f"max {_MAX_HOLDER_CHARS} chars."
                )
            ),
        ],
        reason: Annotated[
            str,
            Field(
                description=(
                    "Operator justification retained in the recovery ledger; "
                    f"max {_MAX_REASON_CHARS} chars."
                )
            ),
        ],
    ) -> ToolResponse:
        """Release one stranded build-use pin. Requires platform operator.

        Conditionally available only when durable worker-termination witnesses are configured.
        Recovery succeeds only when the supplied holder exactly matches the durable use row and
        the exact worker incarnation already has a durable terminated registry row. This tool
        cannot publish termination evidence. Job heartbeat, lease expiry, object absence, and
        identity replacement are never death evidence.
        """
        return await recover_build_use(
            pool, current_context(), use_id=use_id, holder=holder, reason=reason
        )
