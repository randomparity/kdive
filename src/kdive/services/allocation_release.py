"""Shared allocation release mechanics for project and break-glass callers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.state import AllocationState, IllegalTransition
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import config_error as _config_error
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.services import accounting

AuditWriter = Callable[[AsyncConnection, audit.AuditEvent], Awaitable[None]]

_RELEASABLE = (AllocationState.GRANTED, AllocationState.ACTIVE)
_TERMINAL = (AllocationState.RELEASED, AllocationState.EXPIRED, AllocationState.FAILED)


def ctx_audit_writer(ctx: RequestContext) -> AuditWriter:
    """The membership-guarded audit writer used by normal project release."""

    async def _write(conn: AsyncConnection, event: audit.AuditEvent) -> None:
        await audit.record(conn, ctx, event)

    return _write


async def release_with_backstops(
    pool: AsyncConnectionPool,
    uid: UUID,
    *,
    project: str,
    audit_writer: AuditWriter,
) -> ToolResponse:
    """Release an allocation and map transition/reconcile failures to envelopes."""
    async with pool.connection() as conn:
        try:
            return await _release_locked(conn, audit_writer, uid, project=project)
        except IllegalTransition:
            async with pool.connection() as conn2:
                latest = await ALLOCATIONS.get(conn2, uid)
            data = {"current_status": latest.state.value} if latest else {}
            return ToolResponse.failure(str(uid), ErrorCategory.CONFIGURATION_ERROR, data=data)
        except CategorizedError as exc:
            return ToolResponse.failure(str(uid), exc.category)


async def _transition_and_audit(
    conn: AsyncConnection,
    audit_writer: AuditWriter,
    alloc_id: UUID,
    frm: AllocationState,
    to: AllocationState,
    *,
    project: str,
) -> None:
    await ALLOCATIONS.update_state(conn, alloc_id, to)
    await audit_writer(
        conn,
        audit.AuditEvent(
            tool="allocations.release",
            object_kind="allocations",
            object_id=alloc_id,
            transition=f"{frm.value}->{to.value}",
            args={"allocation_id": str(alloc_id)},
            project=project,
        ),
    )


async def _release_locked(
    conn: AsyncConnection, audit_writer: AuditWriter, uid: UUID, *, project: str
) -> ToolResponse:
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, uid),
    ):
        current = await ALLOCATIONS.get(conn, uid)
        if current is None:
            return _config_error(str(uid))
        if current.state in _TERMINAL:
            return ToolResponse.failure(
                str(uid),
                ErrorCategory.STALE_HANDLE,
                suggested_next_actions=["allocations.get"],
                data={"current_status": current.state.value},
            )
        if current.state not in (*_RELEASABLE, AllocationState.RELEASING):
            return ToolResponse.failure(
                str(uid),
                ErrorCategory.CONFIGURATION_ERROR,
                data={"current_status": current.state.value},
            )
        if current.state in _RELEASABLE:
            await _transition_and_audit(
                conn, audit_writer, uid, current.state, AllocationState.RELEASING, project=project
            )
            current = await accounting.stamp_active_ended(conn, current, datetime.now(UTC))
        await _transition_and_audit(
            conn,
            audit_writer,
            uid,
            AllocationState.RELEASING,
            AllocationState.RELEASED,
            project=project,
        )
        await accounting.reconcile(conn, current)
    return ToolResponse.success(str(uid), "released")
