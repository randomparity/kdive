"""Read-side `debug.get_session` / `debug.list_sessions` handlers (ADR-0176).

These recover a debug session handle after context loss. Both require project ``VIEWER``
(symmetry with ``runs.get`` / ``systems.get``) and enforce the no-leak boundary
(ADR-0097 / ADR-0123): a session in a project the caller cannot view is indistinguishable
from an absent one. Neither tool opens/closes a transport or transitions a session — the
state is read as persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.sql import Composable
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import DEBUG_SESSIONS, RUNS
from kdive.domain.capacity.state import DebugSessionState
from kdive.domain.lifecycle import DebugSession
from kdive.log import bind_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

# A session still holds the single-attach transport while `attach`/`live`; once `detached`
# it occupies nothing. The active set is what a recovering agent needs to end or operate.
ACTIVE_SESSION_STATES: tuple[DebugSessionState, ...] = (
    DebugSessionState.ATTACH,
    DebugSessionState.LIVE,
)


@dataclass(frozen=True, slots=True)
class SessionsListRequest:
    """Filter payload for ``debug.list_sessions``."""

    run_id: str | None = None
    system_id: str | None = None
    project: str | None = None
    state: str | None = None
    limit: int = DEFAULT_LIST_LIMIT


def session_envelope(session: DebugSession, *, system_id: UUID | None) -> ToolResponse:
    """Render one debug session; ``status`` is the session's lifecycle state.

    The envelope carries the run/system linkage and transport kind (not the raw transport
    handle, which ops resolve internally from the id). A non-terminal session offers
    ``debug.end_session``; a ``detached`` one only re-reads.
    """
    if session.state in ACTIVE_SESSION_STATES:
        actions = ["debug.get_session", "debug.end_session"]
    else:
        actions = ["debug.get_session"]
    data: dict[str, JsonValue] = {
        "project": session.project,
        "run_id": str(session.run_id),
        "transport": session.transport,
        "system_id": str(system_id) if system_id is not None else None,
    }
    return ToolResponse.success(
        str(session.id), session.state.value, suggested_next_actions=actions, data=data
    )


async def get_session(
    pool: AsyncConnectionPool, ctx: RequestContext, session_id: str
) -> ToolResponse:
    """Return one debug session the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(session_id)
    if uid is None:
        return _config_error(session_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            session = await DEBUG_SESSIONS.get(conn, uid)
            if session is None or session.project not in ctx.projects:
                return _not_found(session_id)
            require_role(ctx, session.project, Role.VIEWER)
            run = await RUNS.get(conn, session.run_id)
        system_id = run.system_id if run is not None else None
        return session_envelope(session, system_id=system_id)


def _viewer_projects(ctx: RequestContext) -> list[str]:
    """Projects the caller may view: a member project with any granted role."""
    return [p for p in ctx.projects if ctx.roles.get(p) is not None]


def _build_filters(
    viewer_projects: list[str],
    *,
    run_id: str | None,
    system_id: str | None,
    project: str | None,
    state: str | None,
) -> tuple[list[Composable], list[object]] | ToolResponse:
    """Translate filter args into SQL clauses + params, or a ``configuration_error``.

    The project clause is always intersected with ``viewer_projects`` — a ``project``
    filter narrows within membership but never widens it, so a cross-project value yields
    zero rows rather than leaking existence.
    """
    clauses: list[Composable] = [sql.SQL("s.project = ANY(%s)")]
    params: list[object] = [viewer_projects]
    if run_id is not None:
        uid = _as_uuid(run_id)
        if uid is None:
            return _config_error(run_id)
        clauses.append(sql.SQL("s.run_id = %s"))
        params.append(uid)
    if system_id is not None:
        uid = _as_uuid(system_id)
        if uid is None:
            return _config_error(system_id)
        clauses.append(sql.SQL("r.system_id = %s"))
        params.append(uid)
    if project is not None:
        clauses.append(sql.SQL("s.project = %s"))
        params.append(project)
    if state is not None:
        try:
            resolved = DebugSessionState(state)
        except ValueError:
            return _config_error(state)
        clauses.append(sql.SQL("s.state = %s"))
        params.append(resolved.value)
    return clauses, params


async def list_sessions(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: SessionsListRequest | None = None,
) -> ToolResponse:
    """List the caller's debug sessions, filterable by run/system/project/state."""
    request = request or SessionsListRequest()
    viewer_projects = _viewer_projects(ctx)
    filters = _build_filters(
        viewer_projects,
        run_id=request.run_id,
        system_id=request.system_id,
        project=request.project,
        state=request.state,
    )
    if isinstance(filters, ToolResponse):
        return filters
    clauses, params = filters
    capped = _clamp_list_limit(request.limit)
    with bind_context(principal=ctx.principal):
        if not viewer_projects:
            return _sessions_collection([])
        query = sql.SQL(
            "SELECT s.*, r.system_id AS join_system_id FROM debug_sessions s "
            "JOIN runs r ON r.id = s.run_id "
            "WHERE {where} ORDER BY s.created_at DESC, s.id LIMIT %s"
        ).format(where=sql.SQL(" AND ").join(clauses))
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, (*params, capped))
            rows = await cur.fetchall()
        return _sessions_collection([_split_system_id(row) for row in rows])


def _split_system_id(row: dict[str, object]) -> tuple[DebugSession, UUID | None]:
    """Separate the joined ``join_system_id`` from the session columns before validation."""
    raw = row.pop("join_system_id")
    system_id = raw if isinstance(raw, UUID) else None
    return DebugSession.model_validate(row), system_id


def _sessions_collection(sessions: list[tuple[DebugSession, UUID | None]]) -> ToolResponse:
    """Render debug sessions into one collection envelope."""
    return ToolResponse.collection(
        "debug_sessions",
        "ok",
        [session_envelope(session, system_id=system_id) for session, system_id in sessions],
        suggested_next_actions=["debug.get_session"],
    )


_ACTIVE_BY_RUN_SQL: LiteralString = (
    "SELECT s.id FROM debug_sessions s "
    "WHERE s.run_id = %s AND s.state IN ('attach', 'live') ORDER BY s.id"
)
_ACTIVE_BY_SYSTEM_SQL: LiteralString = (
    "SELECT s.id FROM debug_sessions s JOIN runs r ON r.id = s.run_id "
    "WHERE r.system_id = %s AND s.state IN ('attach', 'live') ORDER BY s.id"
)


async def active_session_ids_for_run(conn: AsyncConnection, run_id: UUID) -> list[str]:
    """Return the ids of `attach`/`live` debug sessions for one Run (ADR-0176)."""
    return await _active_session_ids(conn, _ACTIVE_BY_RUN_SQL, run_id)


async def active_session_ids_for_system(conn: AsyncConnection, system_id: UUID) -> list[str]:
    """Return the ids of `attach`/`live` debug sessions for any Run on one System."""
    return await _active_session_ids(conn, _ACTIVE_BY_SYSTEM_SQL, system_id)


async def _active_session_ids(
    conn: AsyncConnection, query: LiteralString, value: UUID
) -> list[str]:
    async with conn.cursor() as cur:
        await cur.execute(query, (value,))
        rows = await cur.fetchall()
    return [str(row[0]) for row in rows]
