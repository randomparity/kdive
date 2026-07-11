"""MCP adapters for Investigation lifecycle tools."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._idempotency import keyed_mutation
from kdive.mcp.tools.lifecycle.investigations.common import (
    ExternalRefInput,
    investigation_error_response,
)
from kdive.mcp.tools.lifecycle.investigations.view import envelope_for_investigation
from kdive.security.authz.context import RequestContext
from kdive.services.investigations.common import InvestigationServiceError
from kdive.services.investigations.lifecycle import (
    close_investigation_record,
    open_investigation_record,
)


@dataclass(frozen=True, slots=True)
class InvestigationOpenRequest:
    """Direct-handler request for ``investigations.open``."""

    project: str
    title: str
    description: str | None = None
    external_refs: list[ExternalRefInput] | None = None
    idempotency_key: str | None = None


async def open_investigation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: InvestigationOpenRequest,
) -> ToolResponse:
    """Mint an Investigation (`open`) for the caller's project."""
    async with pool.connection() as conn:

        async def _insert() -> ToolResponse:
            try:
                inv = await open_investigation_record(
                    conn,
                    ctx,
                    project=request.project,
                    title=request.title,
                    description=request.description,
                    external_refs=request.external_refs,
                )
            except InvestigationServiceError as exc:
                return investigation_error_response(exc)
            return await envelope_for_investigation(conn, inv)

        return await keyed_mutation(
            conn,
            idempotency_key=request.idempotency_key,
            principal=ctx.principal,
            project=request.project,
            kind="investigations.open",
            do_work=_insert,
        )


async def close_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    """Drive an Investigation to `closed`."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    try:
        inv = await close_investigation_record(pool, ctx, uid, raw_id=investigation_id)
    except InvestigationServiceError as exc:
        return investigation_error_response(exc)
    async with pool.connection() as conn:
        return await envelope_for_investigation(conn, inv)


__all__ = ["InvestigationOpenRequest", "close_investigation", "open_investigation"]
