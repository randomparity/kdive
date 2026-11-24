"""MCP envelope adapters for transport-neutral idempotency storage."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from hashlib import sha256
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from kdive.domain.errors import CategorizedError
from kdive.jobs.queue import dedup_replay as dedup_replay
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


def object_scoped_kind(
    kind: str, object_id: UUID, arguments: Mapping[str, object] | None = None
) -> str:
    """The ``kind`` for a mutation whose key must not replay against another object or request.

    ``idempotency_keys`` is unique on ``(principal, key)``, so a key reused on a second object
    of the same tool misses the replay lookup and collides on insert, which
    ``keyed_mutation`` surfaces as ``conflict`` (ADR-0193, 2026-11-19 amendment). The canonical
    UUID form keeps one object's spellings in one scope.

    ``arguments`` are the call's effective arguments other than the target and the key: the
    values after parsing, defaulting and clamping, with every optional one present (``None``
    when unset), so an omitted argument and its explicit default produce one kind. They are
    folded in as a short digest of their sorted-key JSON, so a retry that changed one collides
    on insert and resolves to ``conflict`` exactly as a second object does (ADR-0193,
    2026-11-20 argument-digest amendment). ``None`` marks a tool whose only arguments are its
    targets. Values must be JSON scalars, never an object whose ``str`` is not stable.
    """
    scoped = f"{kind}:{object_id}"
    if arguments is None:
        return scoped
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{scoped}:{sha256(canonical.encode()).hexdigest()[:16]}"


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
    "object_scoped_kind",
    "record_envelope",
    "resolve_conflict",
    "resolve_envelope_replay",
    "validate_idempotency_key",
]
