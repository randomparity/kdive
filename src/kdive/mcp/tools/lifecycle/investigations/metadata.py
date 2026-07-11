"""MCP adapters for Investigation metadata mutations."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools.lifecycle.investigations.common import (
    ExternalRefInput,
    ExternalRefKey,
    investigation_error_response,
)
from kdive.mcp.tools.lifecycle.investigations.view import envelope_for_investigation
from kdive.security.authz.context import RequestContext
from kdive.services.investigations.common import InvestigationServiceError
from kdive.services.investigations.metadata import (
    link_external_ref_record,
    set_investigation_record,
    unlink_external_ref_record,
)


async def link_external_ref(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str, ref: ExternalRefInput
) -> ToolResponse:
    """Upsert an external ref onto an Investigation."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    try:
        inv = await link_external_ref_record(pool, ctx, uid, ref, raw_id=investigation_id)
    except InvestigationServiceError as exc:
        return investigation_error_response(exc)
    async with pool.connection() as conn:
        return await envelope_for_investigation(conn, inv)


async def unlink_external_ref(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str, ref: ExternalRefKey
) -> ToolResponse:
    """Remove an external ref by its `(tracker, id)` key."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    try:
        inv = await unlink_external_ref_record(pool, ctx, uid, ref, raw_id=investigation_id)
    except InvestigationServiceError as exc:
        return investigation_error_response(exc)
    async with pool.connection() as conn:
        return await envelope_for_investigation(conn, inv)


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
    try:
        inv = await set_investigation_record(
            pool,
            ctx,
            uid,
            raw_id=investigation_id,
            title=title,
            description=description,
        )
    except InvestigationServiceError as exc:
        return investigation_error_response(exc)
    async with pool.connection() as conn:
        return await envelope_for_investigation(conn, inv)


__all__ = ["link_external_ref", "set_investigation", "unlink_external_ref"]
