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
from kdive.mcp.tools._common import InvalidCursor, clamp_list_limit
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    projects_with_role,
    require_platform_role,
)
from kdive.services.runs.build_use import list_build_uses_page, recover_build_use_in_transaction

_TOOL = "ops.recover_build_use"
_LIST_TOOL = "ops.build_uses_list"
_MAX_HOLDER_BYTES = 512
_MAX_REASON_BYTES = 512
_MAX_BUILD_USE_LIST_LIMIT = 100


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
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> ToolResponse:
    """List a bounded oldest-first set of persistent pins for operator diagnosis."""
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
    except AuthorizationError:
        await audit_platform_denial(pool, ctx, tool=_LIST_TOOL, scope="build-use-recovery")
        return ToolResponse.denied("build-uses", missing_roles=[PlatformRole.PLATFORM_OPERATOR])
    capped = min(clamp_list_limit(limit), _MAX_BUILD_USE_LIST_LIMIT)
    authorized_projects = tuple(sorted(set(projects_with_role(ctx, Role.VIEWER))))
    after = None
    if cursor is not None:
        try:
            after = _decode_ts_uuid_cursor(_LIST_TOOL, cursor)
        except InvalidCursor:
            async with pool.connection() as conn, conn.transaction():
                await audit.record_platform(
                    conn,
                    principal=ctx.principal,
                    agent_session=ctx.agent_session,
                    event=audit.PlatformAuditEvent(
                        tool=_LIST_TOOL,
                        scope="build-use-recovery:invalid_cursor",
                        args={"limit": capped, "project_count": len(authorized_projects)},
                        platform_role=held_platform_roles(ctx),
                        actor=actor_for(ctx),
                    ),
                )
            return _invalid_cursor_error("build-uses")
    async with pool.connection() as conn:
        page = await list_build_uses_page(
            conn,
            authorized_projects=authorized_projects,
            limit=capped,
            after=after,
        )
        async with conn.transaction():
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=_LIST_TOOL,
                    scope="build-use-recovery",
                    args={"limit": capped, "project_count": len(authorized_projects)},
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
    items = [
        ToolResponse.success(
            str(row.use_id),
            "pinned",
            data={
                "investigation_id": str(row.investigation_id),
                "generation": str(row.generation),
                "job_id": str(row.job_id),
                "attempt": str(row.attempt),
                "holder": row.holder_worker_id,
                "created_at": row.created_at.isoformat(),
            },
        )
        for row in page.rows
    ]
    next_cursor = (
        _encode_ts_uuid_cursor(_LIST_TOOL, page.rows[-1].created_at, page.rows[-1].use_id)
        if page.truncated and page.rows
        else None
    )
    return ToolResponse.collection(
        "build-uses",
        "ok",
        items,
        suggested_next_actions=[_TOOL],
        data={"limit": capped, "truncated": page.truncated, "next_cursor": next_cursor},
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
    if (
        not clean_reason
        or not holder.strip()
        or len(clean_reason.encode()) > _MAX_REASON_BYTES
        or len(holder.encode()) > _MAX_HOLDER_BYTES
    ):
        await _audit_refusal(
            pool,
            ctx,
            use_id=use_id,
            holder=holder,
            reason=clean_reason,
            outcome="invalid_input",
        )
        return _failure(use_id, "holder and reason must be non-empty and at most 512 UTF-8 bytes")
    authorized_projects = tuple(sorted(set(projects_with_role(ctx, Role.VIEWER))))
    async with pool.connection() as conn, conn.transaction():
        recovered = await recover_build_use_in_transaction(
            conn,
            use_id,
            authorized_projects=authorized_projects,
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
    return ToolResponse.success(
        str(use_id),
        "recovered",
        suggested_next_actions=[_LIST_TOOL],
        data={"holder": holder},
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the authenticated build-use recovery tool."""

    @app.tool(name=_LIST_TOOL, annotations=_docmeta.read_only(), meta={"maturity": "implemented"})
    async def ops_build_uses_list(
        limit: Annotated[
            int,
            Field(
                description=(
                    "Maximum oldest-first pin rows returned per request; this row-count limit "
                    f"has no clock, applies to one request, and is server-capped at "
                    f"{_MAX_BUILD_USE_LIST_LIMIT}. Higher values are clamped; the service may "
                    "inspect one additional tenant-scoped row to set data.truncated. When "
                    "truncated, call ops.build_uses_list again with data.next_cursor as cursor "
                    "to continue."
                )
            ),
        ] = 50,
        cursor: Annotated[
            str | None,
            Field(
                description=(
                    "Opaque continuation cursor from a prior page's data.next_cursor. A malformed "
                    "or wrong-tool cursor is refused as invalid_cursor; retry with the returned "
                    "cursor in ops.build_uses_list or omit it to restart from the oldest pin."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """List persistent reusable-build pins. Requires platform operator and project viewer.

        Conditionally available only when durable worker-termination witnesses are configured.
        Returns pins only from projects where the caller holds at least viewer; platform authority
        alone grants no tenant-data access and therefore returns an empty list. Keyset-paginated:
        when `data.truncated` is true, pass `data.next_cursor` back as `cursor` for the next page.
        A terminal page, including a valid cursor whose remaining rows disappeared, returns
        `data.truncated=false` and `data.next_cursor=null`.
        A stale job lease is diagnostic context only, never proof that its holder stopped. Pass an
        exact returned use id and holder to `ops.recover_build_use` only after operator review.
        Success returns `object_id=build-uses`, `status=ok`, empty `refs`, and
        `suggested_next_actions=[ops.recover_build_use]`. Its `data.count` is the returned item
        count; `data.limit`, `data.truncated`, and `data.next_cursor` describe the page. Each item
        has its use UUID as `object_id`, `status=pinned`, and `investigation_id`, `generation`,
        `job_id`, `attempt`, `holder`, and PostgreSQL-clock `created_at`. Each request returns the
        bounded oldest-first result described by `limit`. The row-count limit is per request and
        has no reference clock; higher values are clamped, and one additional tenant-scoped row may
        be inspected to establish `data.truncated`. Follow `data.next_cursor` to reach later pins,
        or omit `cursor` to restart diagnostics. On a malformed cursor the tool returns
        `status=error`, `error_category=configuration_error`, and `data.reason=invalid_cursor`; use
        the literal `ops.build_uses_list` action with the last valid cursor or no cursor.
        """
        return await list_build_uses(pool, current_context(), limit=limit, cursor=cursor)

    @app.tool(name=_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def ops_recover_build_use(
        use_id: Annotated[UUID, Field(description="Exact stranded build-use UUID.")],
        holder: Annotated[
            str,
            Field(
                description=(
                    "Exact worker incarnation recorded on that use row; "
                    f"max {_MAX_HOLDER_BYTES} bytes in UTF-8 encoding. The byte limit has no "
                    "clock and applies to this field in one recovery request; an empty or "
                    "oversized value is refused without recovery, so retry with the exact "
                    "bounded holder from ops.build_uses_list in ops.recover_build_use."
                )
            ),
        ],
        reason: Annotated[
            str,
            Field(
                description=(
                    "Operator justification retained in the recovery ledger; "
                    f"max {_MAX_REASON_BYTES} bytes in UTF-8 encoding. The byte limit has no "
                    "clock and applies to this field in one recovery request; an empty or "
                    "oversized value is refused without recovery, so retry with a concise "
                    "reason in ops.recover_build_use."
                )
            ),
        ],
    ) -> ToolResponse:
        """Release one stranded build-use pin. Requires platform operator and project viewer.

        Conditionally available only when durable worker-termination witnesses are configured.
        The caller must hold at least viewer on the pin's project. A missing pin and a pin outside
        the caller's granted projects produce the same refusal shape.
        Recovery succeeds only when the supplied holder exactly matches the durable use row and
        the exact worker incarnation already has a durable terminated registry row. This tool
        cannot publish termination evidence. Job heartbeat, lease expiry, object absence, and
        identity replacement are never death evidence.
        On success it returns the exact use UUID as `object_id`, `status=recovered`,
        `data.holder`, empty `refs`, and `suggested_next_actions=[ops.build_uses_list]` so the
        operator can confirm the remaining pins. A missing, active, mismatched, or foreign use has
        the same refusal shape: `status=error` and `error_category=configuration_error`, with the
        pin retained and literal retry action `ops.recover_build_use` after correcting the facts.
        The holder and reason limits are byte counts in UTF-8 for one recovery request and have no
        reference clock; an empty or oversized field is refused without deletion.
        """
        return await recover_build_use(
            pool, current_context(), use_id=use_id, holder=holder, reason=reason
        )
