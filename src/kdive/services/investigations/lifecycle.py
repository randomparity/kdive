"""Open and close services for Investigations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import IllegalTransition, InvestigationState, SystemState
from kdive.domain.lifecycle.records import Investigation
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, SystemPayload
from kdive.log import bind_context
from kdive.security import audit
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, RoleDenied, require_role
from kdive.serialization import JsonValue
from kdive.services.investigations.common import (
    ExternalRefInput,
    InvestigationErrorReason,
    InvestigationServiceError,
    invalid_text_error,
    parse_external_refs,
    require_summary,
    resolve_contributor_investigation,
    validate_text,
)

# A System is "bound and live" — and so blocks/needs a forced teardown at close — when it names
# this Investigation and has not reached a terminal state (ADR-0441 §7). `torn_down` and `failed`
# are the two terminal `SystemState` sinks; a NULL-investigation System is never considered.
_TERMINAL_SYSTEM_STATES = (SystemState.TORN_DOWN.value, SystemState.FAILED.value)


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


async def _bound_live_systems(conn: AsyncConnection, uid: UUID) -> list[UUID]:
    """Return the ids of non-terminal Systems bound to Investigation ``uid`` (ordered)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM systems WHERE investigation_id = %s AND state <> ALL(%s) ORDER BY id",
            (uid, list(_TERMINAL_SYSTEM_STATES)),
        )
        return [row[0] for row in await cur.fetchall()]


def _require_admin_for_force(ctx: RequestContext, uid: UUID, project: str) -> None:
    """Gate the force-teardown escalation on project ADMIN, matching ``systems.teardown``.

    ``investigations.close`` is a contributor tool, but ``force=True`` tears bound Systems down,
    which is an admin-tier destructive op. Every bound System shares the Investigation's project
    (the same-project binding invariant), so one project-level admin check authorizes the whole
    reap; a same-project contributor is refused here, before any teardown is enqueued.
    """
    try:
        require_role(ctx, project, Role.ADMIN)
    except RoleDenied:
        raise InvestigationServiceError(
            object_id=str(uid),
            reason=InvestigationErrorReason.FORCE_REQUIRES_ADMIN,
            detail="close(force=True) tears down bound Systems and requires admin on the project",
        ) from None


async def _couple_bound_systems(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, *, project: str, force: bool
) -> None:
    """Refuse the close (default) or force-teardown (all-or-nothing) any bound live Systems.

    Runs inside the caller's close transaction so the teardown enqueues and the close itself are
    atomic: an enqueue error rolls the whole close back with zero teardowns enqueued and the
    Investigation unchanged.
    """
    live = await _bound_live_systems(conn, uid)
    if not live:
        return
    ids = [str(system_id) for system_id in live]
    if not force:
        blocking: list[JsonValue] = list(ids)
        raise InvestigationServiceError(
            object_id=str(uid),
            reason=InvestigationErrorReason.BOUND_SYSTEMS_LIVE,
            detail=(
                f"cannot close: {len(ids)} bound System(s) still live "
                f"({', '.join(ids)}); tear them down first or retry with force=true"
            ),
            data={"blocking_systems": blocking},
        )
    _require_admin_for_force(ctx, uid, project)
    authorizing = Authorizing(
        principal=ctx.principal, agent_session=ctx.agent_session, project=project
    )
    for system_id in live:
        await queue.enqueue(
            conn,
            JobKind.TEARDOWN,
            SystemPayload(system_id=str(system_id)),
            authorizing,
            f"{system_id}:teardown",
        )


async def _close_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    *,
    summary: str,
    project: str,
    force: bool,
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
            raise InvestigationServiceError(
                object_id=str(uid),
                reason=InvestigationErrorReason.ABANDONED,
                detail="cannot close an abandoned Investigation",
                data={"current_status": "abandoned"},
            )
        await _couple_bound_systems(conn, ctx, uid, project=project, force=force)
        old = current.state
        updated = await INVESTIGATIONS.update_state(conn, uid, InvestigationState.CLOSED)
        await conn.execute(
            "UPDATE investigations "
            "SET cleanup_pending_at = now(), rootfs_cleanup_pending_at = now(), summary = %s "
            "WHERE id = %s",
            (summary, uid),
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="investigations.close",
                object_kind="investigations",
                object_id=uid,
                transition=f"{old.value}->closed",
                args={"investigation_id": str(uid), "summary_chars": len(summary)},
                project=project,
            ),
        )
    return updated.model_copy(update={"summary": summary})


async def close_investigation_record(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    uid: UUID,
    *,
    raw_id: str,
    summary: str,
    force: bool = False,
) -> Investigation:
    """Drive an Investigation to `closed`, recording a required summary of the work.

    A default close is refused while any bound live System remains (ADR-0441 §7); ``force=True``
    tears those Systems down first (admin-gated, all-or-nothing) and then closes.
    """
    require_summary(raw_id, summary)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await resolve_contributor_investigation(conn, ctx, uid, raw_id)
            try:
                return await _close_locked(
                    conn, ctx, uid, summary=summary, project=inv.project, force=force
                )
            except IllegalTransition:
                async with pool.connection() as conn2:
                    latest = await INVESTIGATIONS.get(conn2, uid)
                if latest is None:
                    raise InvestigationServiceError(
                        object_id=raw_id,
                        reason=InvestigationErrorReason.NOT_FOUND,
                    ) from None
                raise InvestigationServiceError(
                    object_id=raw_id,
                    reason=InvestigationErrorReason.ILLEGAL_STATE,
                    detail=f"Investigation is {latest.state.value}, not closable",
                    data={"current_status": latest.state.value},
                ) from None
