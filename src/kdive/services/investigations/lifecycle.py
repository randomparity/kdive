"""Open and close services for Investigations."""

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
from kdive.security import audit
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role
from kdive.serialization import JsonValue
from kdive.services.investigations.common import (
    ExternalRefInput,
    InvestigationErrorReason,
    InvestigationServiceError,
    invalid_text_error,
    parse_external_refs,
    resolve_contributor_investigation,
    validate_text,
)


async def open_investigation_record(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    project: str,
    title: str,
    description: str | None = None,
    external_refs: list[ExternalRefInput] | None = None,
) -> Investigation:
    """Mint an Investigation (`open`) for the caller's project."""
    require_project(ctx, project)
    require_role(ctx, project, Role.CONTRIBUTOR)
    with bind_context(principal=ctx.principal):
        if not validate_text(title, description):
            raise invalid_text_error(project)
        try:
            refs = parse_external_refs(external_refs)
        except ValidationError, TypeError:
            raise InvestigationServiceError(
                object_id=project,
                reason=InvestigationErrorReason.INVALID_EXTERNAL_REF,
                detail="each external_refs entry must carry a tracker, id, and url",
            ) from None
        now = datetime.now(UTC)
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
        return inv


def _state_error(
    object_id: str,
    reason: InvestigationErrorReason,
    detail: str,
    *,
    data: dict[str, JsonValue] | None = None,
) -> InvestigationServiceError:
    return InvestigationServiceError(
        object_id=object_id,
        reason=reason,
        detail=detail,
        data=data or {},
    )


async def _close_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, *, project: str
) -> Investigation:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await INVESTIGATIONS.get(conn, uid)
        if current is None:
            raise InvestigationServiceError(
                object_id=str(uid),
                reason=InvestigationErrorReason.NOT_FOUND,
            )
        if current.state is InvestigationState.CLOSED:
            return current
        if current.state is InvestigationState.ABANDONED:
            raise _state_error(
                str(uid),
                InvestigationErrorReason.ABANDONED,
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
    return updated


async def close_investigation_record(
    pool: AsyncConnectionPool, ctx: RequestContext, uid: UUID, *, raw_id: str
) -> Investigation:
    """Drive an Investigation to `closed`."""
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await resolve_contributor_investigation(conn, ctx, uid, raw_id)
            try:
                return await _close_locked(conn, ctx, uid, project=inv.project)
            except IllegalTransition:
                async with pool.connection() as conn2:
                    latest = await INVESTIGATIONS.get(conn2, uid)
                if latest is None:
                    raise InvestigationServiceError(
                        object_id=raw_id,
                        reason=InvestigationErrorReason.NOT_FOUND,
                    ) from None
                raise _state_error(
                    raw_id,
                    InvestigationErrorReason.ILLEGAL_STATE,
                    detail=f"Investigation is {latest.state.value}, not closable",
                    data={"current_status": latest.state.value},
                ) from None
