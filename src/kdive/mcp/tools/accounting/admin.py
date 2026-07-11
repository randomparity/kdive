"""Accounting budget and quota administration MCP tools (ADR-0007)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import BUDGETS, QUOTAS
from kdive.domain.accounting.records import Budget, Quota
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.security import audit
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role
from kdive.serialization import JsonValue

_BUDGET_OBJECT_ID = "budget"
_QUOTA_OBJECT_ID = "quota"
# A deterministic placeholder for natural-keyed accounting rows, which have no UUID.
_ACCOUNTING_AUDIT_ID = UUID(int=0)


class QuotaSetRequest(ToolPayload):
    """A project quota update after transport-level scalar parsing."""

    project: str = Field(description="Project to set concurrency caps for.")
    max_concurrent_allocations: int = Field(
        description="Maximum concurrent allocations allowed (>= 0)."
    )
    max_concurrent_systems: int = Field(description="Maximum concurrent Systems allowed (>= 0).")
    max_pending_allocations: int = Field(
        default=0,
        description="Maximum queued (requested) allocations (>= 0); 0 = no queue.",
    )


async def set_budget(
    pool: AsyncConnectionPool, ctx: RequestContext, *, project: str, limit_kcu: object
) -> ToolResponse:
    """Set a project's spend budget ``limit_kcu`` (admin; re-set preserves ``spent_kcu``)."""
    require_project(ctx, project)
    require_role(ctx, project, Role.ADMIN)
    with bind_context(principal=ctx.principal):
        try:
            limit = _parse_non_negative_kcu(limit_kcu)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _BUDGET_OBJECT_ID,
                exc,
                suggested_next_actions=["accounting.set_budget"],
            )
        now = datetime.now(UTC)
        async with pool.connection() as conn, conn.transaction():
            await BUDGETS.upsert(
                conn,
                Budget(project=project, limit_kcu=limit, spent_kcu=Decimal(0), updated_at=now),
            )
            await _audit_set(conn, ctx, project, "set_budget", {"limit_kcu": str(limit)})
            return ToolResponse.success(
                _BUDGET_OBJECT_ID,
                "ok",
                suggested_next_actions=["accounting.usage_project", "allocations.request"],
                data={"project": project, "limit_kcu": str(limit)},
            )


async def set_quota(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    request: QuotaSetRequest,
) -> ToolResponse:
    """Set a project's concurrency caps and the pending-queue cap (admin; ADR-0007 §4,6).

    ``max_pending_allocations`` (ADR-0069) bounds how many ``requested`` rows
    ``on_capacity=queue`` can backlog; it defaults to 0 (queue opt-out) and is distinct from
    ``max_concurrent_allocations`` (the grant cap, which no longer counts ``requested``).
    """
    require_project(ctx, request.project)
    require_role(ctx, request.project, Role.ADMIN)
    with bind_context(principal=ctx.principal):
        if (
            request.max_concurrent_allocations < 0
            or request.max_concurrent_systems < 0
            or request.max_pending_allocations < 0
        ):
            return ToolResponse.failure(
                _QUOTA_OBJECT_ID,
                ErrorCategory.CONFIGURATION_ERROR,
                suggested_next_actions=["accounting.set_quota"],
            )
        now = datetime.now(UTC)
        async with pool.connection() as conn, conn.transaction():
            await QUOTAS.upsert(
                conn,
                Quota(
                    project=request.project,
                    max_concurrent_allocations=request.max_concurrent_allocations,
                    max_concurrent_systems=request.max_concurrent_systems,
                    max_pending_allocations=request.max_pending_allocations,
                    updated_at=now,
                ),
            )
            values: dict[str, JsonValue] = {
                "max_concurrent_allocations": request.max_concurrent_allocations,
                "max_concurrent_systems": request.max_concurrent_systems,
                "max_pending_allocations": request.max_pending_allocations,
            }
            await _audit_set(conn, ctx, request.project, "set_quota", values)
            return ToolResponse.success(
                _QUOTA_OBJECT_ID,
                "ok",
                suggested_next_actions=["accounting.usage_project", "allocations.request"],
                data={"project": request.project, **values},
            )


def _parse_non_negative_kcu(value: object) -> Decimal:
    """Parse ``value`` into a finite, non-negative kcu Decimal."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, DecimalException, ValueError, TypeError) as _exc:
        raise CategorizedError(
            f"limit_kcu {value!r} is not a number",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "limit_kcu", "value": str(value)},
        ) from None
    if not parsed.is_finite() or parsed < 0:
        raise CategorizedError(
            f"limit_kcu {value!r} must be a finite number >= 0",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "limit_kcu", "value": str(value)},
        )
    return parsed


async def _audit_set(
    conn: AsyncConnection,
    ctx: RequestContext,
    project: str,
    tool: str,
    values: Mapping[str, object],
) -> None:
    """Audit an admin set-op under the nil UUID, carrying the project and values in args."""
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=f"accounting.{tool}",
            object_kind="budgets" if tool == "set_budget" else "quotas",
            object_id=_ACCOUNTING_AUDIT_ID,
            transition=f"{tool}:applied",
            args={"project": project, **values},
            project=project,
        ),
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register accounting administration tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="accounting.set_budget",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def accounting_set_budget(
        project: Annotated[str, Field(description="Project to set the spend budget for.")],
        limit_kcu: Annotated[
            float | str,
            Field(description="Budget ceiling in KCU (number or decimal string, >= 0)."),
        ],
    ) -> ToolResponse:
        """Set a project's spend budget limit_kcu; preserves spent_kcu. Requires admin."""
        return await set_budget(pool, current_context(), project=project, limit_kcu=limit_kcu)

    @app.tool(
        name="accounting.set_quota",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def accounting_set_quota(
        request: Annotated[
            QuotaSetRequest,
            Field(description="Project concurrency quota update request."),
        ],
    ) -> ToolResponse:
        """Set a project's concurrency caps and pending-queue cap. Requires admin."""
        return await set_quota(pool, current_context(), request=request)
