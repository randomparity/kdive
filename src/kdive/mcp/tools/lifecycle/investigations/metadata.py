"""Metadata mutation handlers for Investigation MCP tools."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.lifecycle.records import ExternalRef
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import ConfigErrorReason
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools.lifecycle.investigations.common import (
    ExternalRefInput,
    ExternalRefKey,
    get_mutable_investigation_locked,
    invalid_external_refs_error,
    invalid_text_error,
    natural_key,
    parse_external_ref_input,
    refs_jsonb,
    resolve_contributor_investigation,
    validate_text,
)
from kdive.mcp.tools.lifecycle.investigations.view import envelope_for_investigation
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.serialization import JsonValue


async def _link_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, ref: ExternalRef, *, project: str
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await get_mutable_investigation_locked(conn, uid)
        if isinstance(current, ToolResponse):
            return current
        kept = [r for r in current.external_refs if (r.tracker, r.id) != (ref.tracker, ref.id)]
        kept.append(ref)
        await conn.execute(
            "UPDATE investigations SET external_refs = %s WHERE id = %s", (refs_jsonb(kept), uid)
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
        current = await get_mutable_investigation_locked(conn, uid)
        if isinstance(current, ToolResponse):
            return current
        kept = [r for r in current.external_refs if (r.tracker, r.id) != key]
        if len(kept) != len(current.external_refs):
            await conn.execute(
                "UPDATE investigations SET external_refs = %s WHERE id = %s",
                (refs_jsonb(kept), uid),
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
    parsed = parse_external_ref_input(ref, investigation_id)
    if isinstance(parsed, ToolResponse):
        return parsed
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await resolve_contributor_investigation(conn, ctx, uid, investigation_id)
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
    key = natural_key(ref)
    if key is None:
        return invalid_external_refs_error(investigation_id, key_only=True)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await resolve_contributor_investigation(conn, ctx, uid, investigation_id)
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
        current = await get_mutable_investigation_locked(conn, uid)
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
    if not validate_text(title, description):
        return invalid_text_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await resolve_contributor_investigation(conn, ctx, uid, investigation_id)
            if isinstance(inv, ToolResponse):
                return inv
            return await _set_locked(
                conn, ctx, uid, title=title, description=description, project=inv.project
            )
