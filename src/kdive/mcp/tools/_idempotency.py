"""MCP envelope adapters for transport-neutral idempotency storage."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from kdive.domain.errors import CategorizedError
from kdive.mcp.responses import ToolResponse
from kdive.services.idempotency.envelope import (
    StoredResult,
    record_result,
    validate_idempotency_key,
)
from kdive.services.idempotency.envelope import (
    resolve_conflict as _resolve_conflict,
)
from kdive.services.idempotency.envelope import (
    resolve_replay as _resolve_replay,
)


def _stored(envelope: ToolResponse) -> StoredResult:
    return StoredResult(document=envelope.model_dump(mode="json"))


def _envelope(result: StoredResult) -> ToolResponse:
    return ToolResponse.model_validate(result.document)


async def resolve_envelope_replay(
    conn: AsyncConnection, *, principal: str, key: str, kind: str
) -> ToolResponse | None:
    """Return the stored MCP envelope for ``(principal, key, kind)``, if present."""
    replay = await _resolve_replay(conn, principal=principal, key=key, kind=kind)
    if replay is None:
        return None
    return _envelope(replay)


async def record_envelope(
    conn: AsyncConnection,
    *,
    principal: str,
    key: str,
    project: str,
    kind: str,
    envelope: ToolResponse,
) -> None:
    """Persist an MCP envelope through the neutral idempotency service."""
    await record_result(
        conn,
        principal=principal,
        key=key,
        project=project,
        kind=kind,
        result=_stored(envelope),
    )


async def resolve_conflict(
    conn: AsyncConnection, *, principal: str, key: str, kind: str
) -> ToolResponse:
    """Resolve a key collision to the winning MCP envelope or raise conflict."""
    return _envelope(await _resolve_conflict(conn, principal=principal, key=key, kind=kind))


async def keyed_mutation(
    conn: AsyncConnection,
    *,
    idempotency_key: str | None,
    principal: str,
    project: str,
    kind: str,
    do_work: Callable[[], Awaitable[ToolResponse]],
) -> ToolResponse:
    """Run a job-enqueuing MCP mutation under optional replay idempotency."""
    if idempotency_key is None:
        return await do_work()
    try:
        validate_idempotency_key(idempotency_key)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error("idempotency_key", exc)
    replay = await resolve_envelope_replay(
        conn, principal=principal, key=idempotency_key, kind=kind
    )
    if replay is not None:
        return replay
    try:
        async with conn.transaction():
            envelope = await do_work()
            if envelope.error_category is not None:
                return envelope
            await record_envelope(
                conn,
                principal=principal,
                key=idempotency_key,
                project=project,
                kind=kind,
                envelope=envelope,
            )
    except UniqueViolation:
        try:
            return await resolve_conflict(conn, principal=principal, key=idempotency_key, kind=kind)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(idempotency_key, exc)
    return envelope


__all__ = [
    "keyed_mutation",
    "record_envelope",
    "resolve_conflict",
    "resolve_envelope_replay",
    "validate_idempotency_key",
]
