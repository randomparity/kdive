"""Operator recovery of reusable-build pins held by proven-dead worker incarnations."""

from __future__ import annotations

import asyncio
from typing import Annotated, Protocol
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
from kdive.services.runs.worker_incarnations import (
    IncarnationConflict,
    terminate_worker_incarnation,
)

_TOOL = "ops.recover_build_use"
_LIST_TOOL = "ops.build_uses_list"
_MAX_HOLDER_CHARS = 512
_MAX_REASON_CHARS = 512


class WorkerDeathVerifier(Protocol):
    """Authoritative source independent of caller claims and job heartbeat state."""

    def verify_dead(self, worker_incarnation: str) -> str | None: ...


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
    verifier: WorkerDeathVerifier,
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
    # Deployment verifiers may perform a bounded Docker/Kubernetes authority read. Keep that
    # network I/O off FastMCP's event loop; the transaction starts only after death is proven.
    try:
        evidence = await asyncio.to_thread(verifier.verify_dead, holder)
    except Exception:  # noqa: BLE001 -- the deployment authority is an external boundary
        await _audit_refusal(
            pool,
            ctx,
            use_id=use_id,
            holder=holder,
            reason=clean_reason,
            outcome="verifier_error",
        )
        return _failure(use_id, "the authoritative worker-death verifier failed")
    if evidence is None:
        await _audit_refusal(
            pool,
            ctx,
            use_id=use_id,
            holder=holder,
            reason=clean_reason,
            outcome="death_not_proven",
        )
        return _failure(use_id, "the authoritative verifier did not prove this worker dead")
    if len(evidence) > 1024:
        await _audit_refusal(
            pool,
            ctx,
            use_id=use_id,
            holder=holder,
            reason=clean_reason,
            outcome="evidence_oversized",
        )
        return _failure(use_id, "authoritative worker-death evidence exceeds 1024 characters")
    async with pool.connection() as conn:
        match = await (
            await conn.execute(
                "SELECT 1 FROM investigation_build_uses "
                "WHERE use_id = %s AND holder_worker_id = %s",
                (use_id, holder),
            )
        ).fetchone()
    if match is None:
        await _audit_refusal(
            pool,
            ctx,
            use_id=use_id,
            holder=holder,
            reason=clean_reason,
            outcome="use_or_holder_mismatch",
        )
        return _failure(use_id, "build-use pin was absent or its exact holder did not match")
    try:
        async with pool.connection() as conn:
            await terminate_worker_incarnation(conn, holder, "failed")
    except IncarnationConflict:
        await _audit_refusal(
            pool,
            ctx,
            use_id=use_id,
            holder=holder,
            reason=clean_reason,
            outcome="termination_evidence_conflict",
        )
        return _failure(use_id, "the exact worker has no matching durable termination record")
    async with pool.connection() as conn, conn.transaction():
        recovered = await recover_build_use_in_transaction(
            conn,
            use_id,
            confirmed_worker_id=holder,
            recovered_by=ctx.principal,
            evidence=evidence,
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
            return _failure(use_id, "build-use pin was absent or its exact holder did not match")
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


def register(app: FastMCP, pool: AsyncConnectionPool, *, verifier: WorkerDeathVerifier) -> None:
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

        Conditionally available only when the server has an authoritative worker-death verifier.
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

        Conditionally available only when the server has an authoritative worker-death verifier.
        Recovery succeeds only when the supplied holder exactly matches the durable use row and
        the server's configured deployment verifier independently proves that exact worker
        incarnation terminated. Job heartbeat, lease expiry, object absence, and identity
        replacement are never death evidence.
        """
        return await recover_build_use(
            pool, current_context(), verifier, use_id=use_id, holder=holder, reason=reason
        )
