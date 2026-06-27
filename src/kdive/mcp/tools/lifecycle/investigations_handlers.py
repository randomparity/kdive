"""Plain async handlers for the Investigation MCP surface."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import IllegalTransition, InvestigationState
from kdive.domain.lifecycle.records import ExternalRef, Investigation
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, ConfigErrorReason, InvalidCursor
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools._common import paginate as _paginate
from kdive.mcp.tools._idempotency import keyed_mutation
from kdive.mcp.tools.lifecycle.investigations_view import (
    attachments_for_investigations,
    envelope_for_investigation,
    investigation_envelope,
    investigation_list_item,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, projects_with_role, require_role
from kdive.serialization import JsonValue

_TERMINAL_INVESTIGATION = frozenset({InvestigationState.CLOSED, InvestigationState.ABANDONED})
_TITLE_MAX = 200
_DESCRIPTION_MAX = 4096
_INVESTIGATIONS_LIST_TAG = "investigations.list"


class ExternalRefInput(TypedDict):
    """Raw MCP input for a full external tracker reference."""

    tracker: str
    id: str
    url: str


class ExternalRefKey(TypedDict, total=False):
    """Raw MCP input identifying an external reference by natural key."""

    tracker: str
    id: str


def _validate_text(title: str | None, description: str | None) -> bool:
    """Return whether supplied title/description are within their write-boundary bounds."""
    if title is not None and not (1 <= len(title) <= _TITLE_MAX):
        return False
    return description is None or len(description) <= _DESCRIPTION_MAX


def _invalid_text_error(object_id: str) -> ToolResponse:
    """A ``configuration_error`` for an out-of-bounds title/description."""
    return _config_error_reason(
        object_id,
        ConfigErrorReason.INVALID_TEXT,
        detail=(
            f"title must be 1..{_TITLE_MAX} chars and description at most {_DESCRIPTION_MAX} chars"
        ),
    )


def _parse_external_refs(raw: list[ExternalRefInput] | None) -> list[ExternalRef]:
    """Parse + dedup external refs by the ``(tracker, id)`` natural key."""
    if raw is None:
        return []
    by_key: dict[tuple[str, str], ExternalRef] = {}
    for entry in raw:
        ref = ExternalRef.model_validate(entry)
        by_key[(ref.tracker, ref.id)] = ref
    return list(by_key.values())


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
        if not _validate_text(title, description):
            return _invalid_text_error(project)
        normalized_description = description or None
        try:
            refs = _parse_external_refs(external_refs)
        except (ValidationError, TypeError) as _exc:
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
                        description=normalized_description,
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


async def get_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    """Return an Investigation the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
            if inv is None or inv.project not in ctx.projects:
                return _not_found(investigation_id)
            require_role(ctx, inv.project, Role.VIEWER)
            return await envelope_for_investigation(conn, inv)


async def _resolve_contributor_investigation(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, raw_id: str
) -> Investigation | ToolResponse:
    """Resolve a contributor-writable Investigation row or return not-found-shaped error."""
    inv = await INVESTIGATIONS.get(conn, uid)
    if inv is None or inv.project not in ctx.projects:
        return _not_found(raw_id)
    require_role(ctx, inv.project, Role.CONTRIBUTOR)
    return inv


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
            inv = await _resolve_contributor_investigation(conn, ctx, uid, investigation_id)
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


async def _get_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def _get_mutable_investigation_locked(
    conn: AsyncConnection, uid: UUID
) -> Investigation | ToolResponse:
    """Return a locked non-terminal Investigation, or the mutation config error."""
    current = await _get_for_update(conn, uid)
    if current is None:
        return _config_error(str(uid), detail="Investigation no longer exists")
    if current.state in _TERMINAL_INVESTIGATION:
        return _config_error(
            str(uid),
            detail=f"Investigation is {current.state.value}; it cannot be edited",
            data={"current_status": current.state.value},
        )
    return current


def _natural_key(ref: ExternalRefKey) -> tuple[str, str] | None:
    """The ``(tracker, id)`` identity of a ref input; ``None`` if either is missing/blank."""
    try:
        tracker = ref["tracker"]
        rid = ref["id"]
    except KeyError:
        return None
    if not isinstance(tracker, str) or not tracker:
        return None
    if not isinstance(rid, str) or not rid:
        return None
    return (tracker, rid)


def _refs_jsonb(refs: list[ExternalRef]) -> Jsonb:
    return Jsonb([r.model_dump() for r in refs])


async def _link_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, ref: ExternalRef, *, project: str
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await _get_mutable_investigation_locked(conn, uid)
        if isinstance(current, ToolResponse):
            return current
        kept = [r for r in current.external_refs if (r.tracker, r.id) != (ref.tracker, ref.id)]
        kept.append(ref)
        await conn.execute(
            "UPDATE investigations SET external_refs = %s WHERE id = %s", (_refs_jsonb(kept), uid)
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="investigations.link",
                object_kind="investigations",
                object_id=uid,
                transition="link",
                args={"tracker": ref.tracker, "id": ref.id},
                project=project,
            ),
        )
        updated = current.model_copy(update={"external_refs": kept})
    return await envelope_for_investigation(conn, updated)


async def _unlink_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    key: tuple[str, str],
    *,
    project: str,
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await _get_mutable_investigation_locked(conn, uid)
        if isinstance(current, ToolResponse):
            return current
        kept = [r for r in current.external_refs if (r.tracker, r.id) != key]
        if len(kept) != len(current.external_refs):
            await conn.execute(
                "UPDATE investigations SET external_refs = %s WHERE id = %s",
                (_refs_jsonb(kept), uid),
            )
            await audit.record(
                conn,
                ctx,
                audit.AuditEvent(
                    tool="investigations.unlink",
                    object_kind="investigations",
                    object_id=uid,
                    transition="unlink",
                    args={"tracker": key[0], "id": key[1]},
                    project=project,
                ),
            )
        updated = current.model_copy(update={"external_refs": kept})
    return await envelope_for_investigation(conn, updated)


async def link_external_ref(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str, ref: ExternalRefInput
) -> ToolResponse:
    """Upsert an external ref onto an Investigation (keyed on `(tracker, id)`)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    try:
        parsed = ExternalRef.model_validate(ref)
    except ValidationError:
        return _config_error_reason(
            investigation_id,
            ConfigErrorReason.INVALID_EXTERNAL_REF,
            detail="ref must carry a tracker, id, and url",
        )
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await _resolve_contributor_investigation(conn, ctx, uid, investigation_id)
            if isinstance(inv, ToolResponse):
                return inv
            return await _link_locked(conn, ctx, uid, parsed, project=inv.project)


async def unlink_external_ref(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str, ref: ExternalRefKey
) -> ToolResponse:
    """Remove an external ref by its `(tracker, id)` key (idempotent; `url` ignored)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    key = _natural_key(ref)
    if key is None:
        return _config_error_reason(
            investigation_id,
            ConfigErrorReason.INVALID_EXTERNAL_REF,
            detail="ref key must carry a non-empty tracker and id",
        )
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await _resolve_contributor_investigation(conn, ctx, uid, investigation_id)
            if isinstance(inv, ToolResponse):
                return inv
            return await _unlink_locked(conn, ctx, uid, key, project=inv.project)


async def _set_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    *,
    title: str | None,
    description: str | None,
    project: str,
) -> ToolResponse:
    """Apply a title/description edit under the per-Investigation lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await _get_mutable_investigation_locked(conn, uid)
        if isinstance(current, ToolResponse):
            return current
        new_title = title if title is not None else current.title
        new_description = current.description if description is None else (description or None)
        audit_args: dict[str, JsonValue] = {}
        if title is not None:
            audit_args["title"] = title
        if description is not None:
            audit_args["description"] = "cleared" if description == "" else "set"
        await conn.execute(
            "UPDATE investigations SET title = %s, description = %s WHERE id = %s",
            (new_title, new_description, uid),
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="investigations.set",
                object_kind="investigations",
                object_id=uid,
                transition="set",
                args=audit_args,
                project=project,
            ),
        )
        updated = current.model_copy(update={"title": new_title, "description": new_description})
    return await envelope_for_investigation(conn, updated)


async def set_investigation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    investigation_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
) -> ToolResponse:
    """Edit an Investigation's title and/or description."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    if title is None and description is None:
        return _config_error_reason(
            investigation_id,
            ConfigErrorReason.MISSING_REQUIRED_FIELD,
            detail="set requires at least one of title or description",
        )
    if not _validate_text(title, description):
        return _invalid_text_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await _resolve_contributor_investigation(conn, ctx, uid, investigation_id)
            if isinstance(inv, ToolResponse):
                return inv
            return await _set_locked(
                conn, ctx, uid, title=title, description=description, project=inv.project
            )


async def _fetch_investigation_rows(
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


async def list_investigations(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str | None = None,
    state: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> ToolResponse:
    """List the caller's viewer-project Investigations, newest-first."""
    resolved_state: InvestigationState | None = None
    if state is not None:
        try:
            resolved_state = InvestigationState(state)
        except ValueError:
            return _config_error_reason(
                "investigations.list",
                ConfigErrorReason.INVALID_STATE,
                accepted_values=[s.value for s in InvestigationState],
                detail=f"state {state!r} is not a valid Investigation state",
            )
    capped = _clamp_list_limit(limit)
    after = None
    if cursor:
        try:
            after = _decode_ts_uuid_cursor(_INVESTIGATIONS_LIST_TAG, cursor)
        except InvalidCursor:
            return _invalid_cursor_error("investigations.list")
    with bind_context(principal=ctx.principal):
        viewer_projects = tuple(projects_with_role(ctx, Role.VIEWER))
        if project is not None:
            viewer_projects = tuple(p for p in viewer_projects if p == project)
        async with pool.connection() as conn:
            rows = await _fetch_investigation_rows(
                conn, viewer_projects, resolved_state, limit=capped + 1, after=after
            )
            kept, truncated = _paginate(rows, capped)
            next_cursor = (
                _encode_ts_uuid_cursor(
                    _INVESTIGATIONS_LIST_TAG, kept[-1]["created_at"], kept[-1]["id"]
                )
                if truncated and kept
                else None
            )
            render_queue = [investigation_list_item(row) for row in kept]
            investigations = [item for item in render_queue if not isinstance(item, ToolResponse)]
            attachments = await attachments_for_investigations(
                conn, [inv.id for inv in investigations]
            )
            items = [
                item
                if isinstance(item, ToolResponse)
                else investigation_envelope(item, attachments[item.id])
                for item in render_queue
            ]
        return ToolResponse.collection(
            "investigations",
            "ok",
            items,
            suggested_next_actions=["investigations.get", "investigations.open"],
            data={"truncated": truncated, "next_cursor": next_cursor},
        )
