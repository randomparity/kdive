"""Read-only `runs.list` MCP handler (ADR-0198).

The Run catalog peer of `runs.get`: viewer-gated, project-scoped (no-leak), filterable by
`system_id` / `investigation_id` / `state`, keyset-paginated over `(created_at, id) DESC`
through the ADR-0192 cursor helpers. Mirrors `systems/view.py::list_systems` minus the
allocation/resource join — every filter and sort column is a direct `runs` column.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.sql import Composable
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import RunState
from kdive.domain.lifecycle import Run
from kdive.log import bind_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, ConfigErrorReason, InvalidCursor
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import paginate as _paginate
from kdive.mcp.tools.lifecycle.runs.common import envelope_for_run
from kdive.security.authz.context import RequestContext

_RUNS_LIST_TAG = "runs.list"


@dataclass(frozen=True, slots=True)
class RunsListRequest:
    """Filter payload for ``runs.list``."""

    system_id: str | None = None
    investigation_id: str | None = None
    state: str | None = None
    limit: int = DEFAULT_LIST_LIMIT
    cursor: str | None = None


def _viewer_projects(ctx: RequestContext) -> list[str]:
    """Projects the caller may view: a member project with any granted role."""
    return [p for p in ctx.projects if ctx.roles.get(p) is not None]


def _build_filters(
    viewer_projects: list[str],
    *,
    system_id: str | None,
    investigation_id: str | None,
    state: str | None,
) -> tuple[list[Composable], list[object]] | ToolResponse:
    """Translate filter args into SQL clauses + params, or a ``configuration_error``."""
    clauses: list[Composable] = [sql.SQL("project = ANY(%s)")]
    params: list[object] = [viewer_projects]
    if system_id is not None:
        uid = _as_uuid(system_id)
        if uid is None:
            return _invalid_uuid_error("system_id", system_id)
        clauses.append(sql.SQL("system_id = %s"))
        params.append(uid)
    if investigation_id is not None:
        uid = _as_uuid(investigation_id)
        if uid is None:
            return _invalid_uuid_error("investigation_id", investigation_id)
        clauses.append(sql.SQL("investigation_id = %s"))
        params.append(uid)
    if state is not None:
        try:
            resolved = RunState(state)
        except ValueError:
            return _config_error_reason(
                state,
                ConfigErrorReason.INVALID_STATE,
                accepted_values=[s.value for s in RunState],
                detail=f"state {state!r} is not a valid Run state",
            )
        clauses.append(sql.SQL("state = %s"))
        params.append(resolved.value)
    return clauses, params


async def list_runs(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunsListRequest | None = None,
) -> ToolResponse:
    """List the caller's Runs, filterable by system, investigation, and state.

    Validation precedes scoping: a malformed filter or cursor is a ``configuration_error``
    regardless of the caller's project grants, so the error path never depends on what the
    caller may see (ADR-0198). An empty viewer-project set then short-circuits to an empty
    collection without a query.
    """
    request = request or RunsListRequest()
    viewer_projects = _viewer_projects(ctx)
    filters = _build_filters(
        viewer_projects,
        system_id=request.system_id,
        investigation_id=request.investigation_id,
        state=request.state,
    )
    if isinstance(filters, ToolResponse):
        return filters
    clauses, params = filters
    capped = _clamp_list_limit(request.limit)
    after = None
    if request.cursor:
        try:
            after = _decode_ts_uuid_cursor(_RUNS_LIST_TAG, request.cursor)
        except InvalidCursor:
            return _invalid_cursor_error("runs")
    with bind_context(principal=ctx.principal):
        if not viewer_projects:
            return _runs_collection([], truncated=False, next_cursor=None)
        if after is not None:
            clauses.append(sql.SQL("(created_at, id) < (%s, %s)"))
            params.extend(after)
        query = sql.SQL(
            "SELECT * FROM runs WHERE {where} ORDER BY created_at DESC, id DESC LIMIT %s"
        ).format(where=sql.SQL(" AND ").join(clauses))
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, (*params, capped + 1))
            rows = await cur.fetchall()
        kept, truncated = _paginate(rows, capped)
        next_cursor = (
            _encode_ts_uuid_cursor(_RUNS_LIST_TAG, kept[-1]["created_at"], kept[-1]["id"])
            if truncated and kept
            else None
        )
        return _runs_collection(
            [Run.model_validate(row) for row in kept],
            truncated=truncated,
            next_cursor=next_cursor,
        )


def _runs_collection(
    runs: list[Run],
    *,
    truncated: bool,
    next_cursor: str | None,
) -> ToolResponse:
    """Render Runs into one collection envelope with the pagination payload."""
    data: dict[str, JsonValue] = {"truncated": truncated, "next_cursor": next_cursor}
    return ToolResponse.collection(
        "runs",
        "ok",
        [envelope_for_run(run) for run in runs],
        suggested_next_actions=["runs.get", "runs.create"],
        data=data,
    )
