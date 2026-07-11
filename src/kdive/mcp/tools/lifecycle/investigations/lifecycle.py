"""Open and close handlers for Investigation MCP tools."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import IllegalTransition, InvestigationState
from kdive.domain.lifecycle.records import Investigation
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import ConfigErrorReason
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools._idempotency import keyed_mutation
from kdive.mcp.tools.lifecycle.investigations.common import (
    ExternalRefInput,
    invalid_text_error,
    parse_external_refs,
    resolve_contributor_investigation,
    validate_text,
)
from kdive.mcp.tools.lifecycle.investigations.view import envelope_for_investigation
from kdive.security import audit
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role


async def open_investigation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    title: str,
    description: str | None = None,
    external_refs: list[ExternalRefInput] | None = None,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Mint an Investigation (`open`) for the caller's project."""
    require_project(ctx, project)
    require_role(ctx, project, Role.CONTRIBUTOR)
    with bind_context(principal=ctx.principal):
        if not validate_text(title, description):
            return invalid_text_error(project)
        try:
            refs = parse_external_refs(external_refs)
        except ValidationError, TypeError:
            return _config_error_reason(
                project,
                ConfigErrorReason.INVALID_EXTERNAL_REF,
                detail="each external_refs entry must carry a tracker, id, and url",
            )
        now = datetime.now(UTC)
        async with pool.connection() as conn:

            async def _insert() -> ToolResponse:
                inv = await INVESTIGATIONS.insert(
                    conn,
                    Investigation(
                        id=uuid4(),
                        created_at=now,
                        updated_at=now,
                        principal=ctx.principal,
                        agent_session=ctx.agent_session,
                        project=project,
                        title=title,
                        description=description or None,
                        external_refs=refs,
                        state=InvestigationState.OPEN,
                    ),
                )
                await audit.record(
                    conn,
                    ctx,
                    audit.AuditEvent(
                        tool="investigations.open",
                        object_kind="investigations",
                        object_id=inv.id,
                        transition="->open",
                        args={"project": project, "title": title},
                        project=project,
                    ),
                )
                return await envelope_for_investigation(conn, inv)

            return await keyed_mutation(
                conn,
                idempotency_key=idempotency_key,
                principal=ctx.principal,
                project=project,
                kind="investigations.open",
                do_work=_insert,
            )


async def _close_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, *, project: str
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await INVESTIGATIONS.get(conn, uid)
        if current is None:
            return _not_found(str(uid))
        if current.state is InvestigationState.CLOSED:
            return await envelope_for_investigation(conn, current)
        if current.state is InvestigationState.ABANDONED:
            return _config_error(
                str(uid),
                detail="cannot close an abandoned Investigation",
                data={"current_status": "abandoned"},
            )
        old = current.state
        updated = await INVESTIGATIONS.update_state(conn, uid, InvestigationState.CLOSED)
        await conn.execute(
            "UPDATE investigations SET cleanup_pending_at = now() WHERE id = %s", (uid,)
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="investigations.close",
                object_kind="investigations",
                object_id=uid,
                transition=f"{old.value}->closed",
                args={"investigation_id": str(uid)},
                project=project,
            ),
        )
    return await envelope_for_investigation(conn, updated)


async def close_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    """Drive an Investigation to `closed` (idempotent on an already-`closed` row)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await resolve_contributor_investigation(conn, ctx, uid, investigation_id)
            if isinstance(inv, ToolResponse):
                return inv
            try:
                return await _close_locked(conn, ctx, uid, project=inv.project)
            except IllegalTransition:
                async with pool.connection() as conn2:
                    latest = await INVESTIGATIONS.get(conn2, uid)
                if latest is None:
                    return _not_found(investigation_id)
                return _config_error(
                    investigation_id,
                    detail=f"Investigation is {latest.state.value}, not closable",
                    data={"current_status": latest.state.value},
                )
