"""The `investigations.*` MCP tools — the Investigation campaign surface (ADR-0026).

Thin FastMCP wrappers over plain async handlers (pool + ctx injected; tested directly).
`open` mints an Investigation (`open`); `close` drives it to `closed`; `link`/`unlink`
mutate the `external_refs` jsonb under a per-Investigation advisory lock, keyed on the
`(tracker, id)` natural key (link upserts, unlink removes-if-present — both idempotent).
`get`/the mutators render through `_envelope_for_investigation` (every Investigation state
is a non-failure status, so no failure mapping is needed). RBAC: mutations require
`contributor`; reads require `viewer` on the owning project. Authz denials raise
(ADR-0020: no authz ErrorCategory).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import Field, ValidationError

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import IllegalTransition, InvestigationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle import ExternalRef, Investigation
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
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
from kdive.security import audit
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, projects_with_role, require_role
from kdive.serialization import JsonValue
from kdive.services.idempotency.envelope import keyed_mutation

_log = logging.getLogger(__name__)
type InvestigationListItem = Investigation | ToolResponse

_TERMINAL_INVESTIGATION = frozenset({InvestigationState.CLOSED, InvestigationState.ABANDONED})

_TITLE_MAX = 200
_DESCRIPTION_MAX = 4096


def _validate_text(title: str | None, description: str | None) -> bool:
    """Return whether supplied title/description are within their write-boundary bounds.

    A ``None`` field is "not supplied" and is not checked. ``title`` (when supplied) must be
    1..=200 chars; ``description`` (when supplied) must be 0..=4096 chars. Bounds live here, not on
    the model, so reading a pre-existing out-of-bound row never raises (ADR-0135).
    """
    if title is not None and not (1 <= len(title) <= _TITLE_MAX):
        return False
    return description is None or len(description) <= _DESCRIPTION_MAX


def _invalid_text_error(object_id: str) -> ToolResponse:
    """A ``configuration_error`` for an out-of-bounds title/description (ADR-0174)."""
    return _config_error_reason(
        object_id,
        ConfigErrorReason.INVALID_TEXT,
        detail=(
            f"title must be 1..{_TITLE_MAX} chars and description at most {_DESCRIPTION_MAX} chars"
        ),
    )


class ExternalRefInput(TypedDict):
    """Raw MCP input for a full external tracker reference."""

    tracker: str
    id: str
    url: str


class ExternalRefKey(TypedDict, total=False):
    """Raw MCP input identifying an external reference by natural key."""

    tracker: str
    id: str


class _InvestigationAttachments(TypedDict):
    runs: list[JsonValue]
    systems: list[JsonValue]


async def _attached_runs_and_systems(
    conn: AsyncConnection, investigation_id: UUID
) -> tuple[list[JsonValue], list[JsonValue]]:
    """Return ``(run_ids, distinct_system_ids)`` for an Investigation's attached Runs (ADR-0143).

    Runs are ordered ``created_at, id`` (oldest first, stable); systems are deduplicated in
    first-seen order over that run set. No project predicate: a Run's project equals its
    Investigation's (enforced at ``runs.create``) and the Investigation row was already resolved
    under the caller's ``viewer`` scope, so its runs are in-scope by construction. Both lists are
    typed ``JsonValue`` so they drop straight into the response ``data`` map (ADR-0143).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, system_id FROM runs WHERE investigation_id = %s ORDER BY created_at, id",
            (investigation_id,),
        )
        rows = await cur.fetchall()
    run_ids: list[JsonValue] = [str(run_id) for run_id, _ in rows]
    seen: set[str] = set()
    system_ids: list[JsonValue] = []
    for _, system_id in rows:
        sid = str(system_id)
        if sid not in seen:
            seen.add(sid)
            system_ids.append(sid)
    return run_ids, system_ids


async def _attachments_for_investigations(
    conn: AsyncConnection, investigation_ids: list[UUID]
) -> dict[UUID, _InvestigationAttachments]:
    """Batch-load attached Runs and distinct Systems for Investigation envelopes."""
    attachments: dict[UUID, _InvestigationAttachments] = {
        uid: {"runs": [], "systems": []} for uid in investigation_ids
    }
    if not investigation_ids:
        return attachments
    seen_systems: dict[UUID, set[str]] = {uid: set() for uid in investigation_ids}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT investigation_id, id, system_id FROM runs "
            "WHERE investigation_id = ANY(%s) ORDER BY investigation_id, created_at, id",
            (investigation_ids,),
        )
        rows = await cur.fetchall()
    for raw_investigation_id, run_id, system_id in rows:
        investigation_id = UUID(str(raw_investigation_id))
        attached = attachments[investigation_id]
        attached["runs"].append(str(run_id))
        sid = str(system_id)
        if sid not in seen_systems[investigation_id]:
            seen_systems[investigation_id].add(sid)
            attached["systems"].append(sid)
    return attachments


def _investigation_envelope(
    inv: Investigation, attachments: _InvestigationAttachments
) -> ToolResponse:
    """Render an Investigation; every state is a non-failure status (ADR-0026 §6)."""
    if inv.state in _TERMINAL_INVESTIGATION:
        actions = ["investigations.get"]
    else:
        actions = ["investigations.get", "investigations.close", "runs.create"]
    data: dict[str, JsonValue] = {
        "project": inv.project,
        "title": inv.title,
        "description": inv.description,
        "external_refs": [r.model_dump() for r in inv.external_refs],
        "state": inv.state.value,
        "last_run_at": inv.last_run_at.isoformat() if inv.last_run_at else None,
        "runs": attachments["runs"],
        "systems": attachments["systems"],
    }
    return ToolResponse.success(
        str(inv.id), inv.state.value, suggested_next_actions=actions, data=data
    )


async def _envelope_for_investigation(conn: AsyncConnection, inv: Investigation) -> ToolResponse:
    """Load attachments and render a single Investigation envelope."""
    run_ids, system_ids = await _attached_runs_and_systems(conn, inv.id)
    return _investigation_envelope(inv, {"runs": run_ids, "systems": system_ids})


def _parse_external_refs(raw: list[ExternalRefInput] | None) -> list[ExternalRef]:
    """Parse + dedup external refs by the ``(tracker, id)`` natural key (last-wins).

    Raises:
        ValidationError / TypeError: A malformed entry or a non-list container.
    """
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
        normalized_description = description or None  # "" -> None on open (ADR-0135 §2)
        try:
            refs = _parse_external_refs(external_refs)
        except ValidationError, TypeError:
            return _config_error_reason(
                project,
                ConfigErrorReason.INVALID_EXTERNAL_REF,
                detail="each external_refs entry must carry a tracker, id, and url",
            )
        now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
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
                return await _envelope_for_investigation(conn, inv)

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
            return await _envelope_for_investigation(conn, inv)


async def _resolve_contributor_investigation(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, raw_id: str
) -> Investigation | ToolResponse:
    """Resolve a contributor-writable Investigation row or return the not-found-shaped error."""
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
            return await _envelope_for_investigation(conn, current)  # idempotent: already closed
        if current.state is InvestigationState.ABANDONED:
            return _config_error(
                str(uid),
                detail="cannot close an abandoned Investigation",
                data={"current_status": "abandoned"},
            )
        old = current.state
        updated = await INVESTIGATIONS.update_state(conn, uid, InvestigationState.CLOSED)
        # Mark for deferred build-artifact cleanup; the reconciler sweep reclaims run-owned build
        # artifacts after a grace window (ADR-0234 §4, #768). Never a synchronous delete here.
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
    return await _envelope_for_investigation(conn, updated)


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
                # Backstop for an interleaving the lock did not cover (e.g. a future
                # non-advisory writer). Caught OUTSIDE the rolled-back transaction; re-read.
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
    return await _envelope_for_investigation(conn, updated)


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
    return await _envelope_for_investigation(conn, updated)


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
    """Apply a title/description edit under the per-Investigation lock (ADR-0135).

    The lock + ``FOR UPDATE`` read serialize every title/description writer, so writing both
    columns from the locked ``current`` snapshot cannot clobber a concurrent edit.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await _get_mutable_investigation_locked(conn, uid)
        if isinstance(current, ToolResponse):
            return current
        new_title = title if title is not None else current.title
        # description: None -> leave unchanged; "" -> NULL (clear); else the new value.
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
    return await _envelope_for_investigation(conn, updated)


async def set_investigation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    investigation_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
) -> ToolResponse:
    """Edit an Investigation's title and/or description (partial, value-based; ADR-0135)."""
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
    """Fetch a keyset page of raw investigation rows (``limit + 1`` for truncation, ADR-0192)."""
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


def _investigation_list_item(row: dict[str, Any]) -> InvestigationListItem:
    try:
        return Investigation.model_validate(row)
    except ValueError:
        object_id = row.get("id")
        _log.warning(
            "investigation %s violates the response invariant; degraded",
            object_id if object_id is not None else "<missing>",
            exc_info=True,
        )
        return _investigation_row_error(object_id)


def _investigation_row_error(object_id: object | None) -> ToolResponse:
    return ToolResponse.failure(
        str(object_id) if object_id is not None else "investigations.list",
        ErrorCategory.CONFIGURATION_ERROR,
    )


_INVESTIGATIONS_LIST_TAG = "investigations.list"


async def list_investigations(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str | None = None,
    state: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> ToolResponse:
    """List the caller's viewer-project Investigations, newest-first (ADR-0135, ADR-0192).

    Keyset-paginated: fetches one row past ``limit`` to set ``data.truncated`` /
    ``data.next_cursor`` from the last kept row's ``(created_at, id)``.
    """
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
            render_queue = [_investigation_list_item(row) for row in kept]
            investigations = [item for item in render_queue if not isinstance(item, ToolResponse)]
            attachments = await _attachments_for_investigations(
                conn, [inv.id for inv in investigations]
            )
            items = [
                item
                if isinstance(item, ToolResponse)
                else _investigation_envelope(item, attachments[item.id])
                for item in render_queue
            ]
        return ToolResponse.collection(
            "investigations",
            "ok",
            items,
            suggested_next_actions=["investigations.get", "investigations.open"],
            data={"truncated": truncated, "next_cursor": next_cursor},
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `investigations.*` tools on ``app``, bound to ``pool``."""
    _register_investigations_open(app, pool)
    _register_investigations_get(app, pool)
    _register_investigations_close(app, pool)
    _register_investigations_link(app, pool)
    _register_investigations_unlink(app, pool)
    _register_investigations_set(app, pool)
    _register_investigations_list(app, pool)


def _register_investigations_open(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.open",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_open(
        project: Annotated[str, Field(description="Project to create the Investigation under.")],
        title: Annotated[str, Field(description="Human-readable title (1..=200 chars).")],
        description: Annotated[
            str | None,
            Field(description="Optional free-form description for reporting (<=4096 chars)."),
        ] = None,
        external_refs: Annotated[
            list[ExternalRefInput] | None,
            Field(description="Optional external tracker refs (each with tracker, id, url)."),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Open an investigation."""
        return await open_investigation(
            pool,
            current_context(),
            project=project,
            title=title,
            description=description,
            external_refs=external_refs,
            idempotency_key=idempotency_key,
        )


def _register_investigations_get(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def investigations_get(
        investigation_id: Annotated[str, Field(description="The Investigation to render.")],
    ) -> ToolResponse:
        """Return one investigation."""
        return await get_investigation(pool, current_context(), investigation_id)


def _register_investigations_close(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.close",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_close(
        investigation_id: Annotated[
            str, Field(description="The Investigation to drive to closed.")
        ],
    ) -> ToolResponse:
        """Close an investigation."""
        return await close_investigation(pool, current_context(), investigation_id)


def _register_investigations_link(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.link",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_link(
        investigation_id: Annotated[str, Field(description="The Investigation to add the ref to.")],
        ref: Annotated[
            ExternalRefInput,
            Field(description="External ref to upsert, with tracker, id, and url."),
        ],
    ) -> ToolResponse:
        """Link a run to an investigation."""
        return await link_external_ref(pool, current_context(), investigation_id, ref)


def _register_investigations_unlink(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.unlink",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_unlink(
        investigation_id: Annotated[
            str, Field(description="The Investigation to remove the ref from.")
        ],
        ref: Annotated[
            ExternalRefKey,
            Field(description="Ref to remove; only tracker and id are used as the key."),
        ],
    ) -> ToolResponse:
        """Unlink a run from an investigation."""
        return await unlink_external_ref(pool, current_context(), investigation_id, ref)


def _register_investigations_set(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.set",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_set(
        investigation_id: Annotated[str, Field(description="The Investigation to edit.")],
        title: Annotated[
            str | None,
            Field(description="New title (1..=200 chars); omit to leave unchanged."),
        ] = None,
        description: Annotated[
            str | None,
            Field(description='New description (<=4096); "" clears it; omit to leave unchanged.'),
        ] = None,
    ) -> ToolResponse:
        """Edit a non-terminal Investigation's title and/or free-form description."""
        return await set_investigation(
            pool, current_context(), investigation_id, title=title, description=description
        )


def _register_investigations_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def investigations_list(
        project: Annotated[
            str | None,
            Field(description="Restrict to one project you can view; omit for all."),
        ] = None,
        state: Annotated[
            str | None,
            Field(description="Filter by state (open/active/closed/abandoned)."),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum rows returned (capped at 200).")
        ] = DEFAULT_LIST_LIMIT,
        cursor: Annotated[
            str | None,
            Field(description="Opaque continuation cursor from a prior page's next_cursor."),
        ] = None,
    ) -> ToolResponse:
        """List the Investigations you can view, newest-first, for reporting.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page.
        """
        return await list_investigations(
            pool, current_context(), project=project, state=state, limit=limit, cursor=cursor
        )
