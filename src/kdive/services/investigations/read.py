"""Read services for Investigations."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import InvestigationState
from kdive.domain.lifecycle.records import Investigation
from kdive.log import bind_context
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, projects_with_role, require_role
from kdive.services.investigations.common import (
    InvestigationErrorReason,
    InvestigationServiceError,
)


async def get_investigation_record(
    pool: AsyncConnectionPool, ctx: RequestContext, uid: UUID, *, raw_id: str
) -> Investigation:
    """Return an Investigation the caller's project owns."""
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
            if inv is None or inv.project not in ctx.projects:
                raise InvestigationServiceError(
                    object_id=raw_id,
                    reason=InvestigationErrorReason.NOT_FOUND,
                )
            require_role(ctx, inv.project, Role.VIEWER)
            return inv


async def fetch_investigation_rows(
    conn: AsyncConnection,
    projects: tuple[str, ...],
    state: InvestigationState | None,
    *,
    limit: int,
    after: tuple[datetime, UUID] | None,
) -> list[dict[str, Any]]:
    """Fetch a keyset page of raw investigation rows."""
    query = "SELECT * FROM investigations WHERE project = ANY(%s)"
    params: list[object] = [list(projects)]
    if state is not None:
        query += " AND state = %s"
        params.append(state.value)
    if after is not None:
        query += " AND (created_at, id) < (%s, %s)"
        params.extend(after)
    query += " ORDER BY created_at DESC, id DESC LIMIT %s"
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


async def list_investigation_rows(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str | None,
    state: InvestigationState | None,
    limit: int,
    after: tuple[datetime, UUID] | None,
) -> list[dict[str, Any]]:
    """List the caller's viewer-project Investigation rows, newest-first."""
    with bind_context(principal=ctx.principal):
        viewer_projects = tuple(projects_with_role(ctx, Role.VIEWER))
        if project is not None:
            viewer_projects = tuple(p for p in viewer_projects if p == project)
        async with pool.connection() as conn:
            return await fetch_investigation_rows(
                conn, viewer_projects, state, limit=limit, after=after
            )
