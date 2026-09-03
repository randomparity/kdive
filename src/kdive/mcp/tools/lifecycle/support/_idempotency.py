"""MCP envelope adapters for transport-neutral idempotency storage."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import Job
from kdive.jobs import queue
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


# States `queue.enqueue` resets to a fresh queued attempt, per policy. Its `NEVER` default
# recycles nothing, so under it *every* prior row is returned unchanged — including a terminal
# one. Derived from the same enum `enqueue` branches on, so a site cannot hand-list a narrower
# set than its own policy implies (#2117 review: the `vmcore.fetch` probe did exactly that).
_RECYCLED: dict[queue.JobRecyclePolicy, frozenset[JobState]] = {
    queue.JobRecyclePolicy.NEVER: frozenset(),
    queue.JobRecyclePolicy.TERMINAL: frozenset({JobState.FAILED, JobState.SUCCEEDED}),
    queue.JobRecyclePolicy.TERMINAL_OR_CANCELED: frozenset(
        {JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELED}
    ),
}


async def dedup_replay(
    conn: AsyncConnection,
    dedup_key: str,
    *,
    recycle: queue.JobRecyclePolicy = queue.JobRecyclePolicy.NEVER,
) -> Job | None:
    """The job a repeat ``enqueue`` on ``dedup_key`` would return unchanged, else ``None``.

    ``None`` means the call would commit fresh work — a new row, or a recycled terminal one.

    This is what an unkeyed repeat call replays on. ``keyed_mutation`` short-circuits to
    ``do_work()`` when ``idempotency_key is None``, so on that path there is no stored envelope
    and the dedup key is the only replay there is. Sites that admit against the external-boot
    matrix (ADR-0583) must consult this *before* their guard, or an activation that appeared
    after the work was enqueued turns an agent's poll into a refusal while the job it is
    polling stays queued and runs.

    Pass the same ``recycle`` the site's own ``enqueue`` passes; the states are derived from it.
    """
    prior = await queue.get_by_dedup_key(conn, dedup_key)
    if prior is None or prior.state in _RECYCLED[recycle]:
        return None
    return prior


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
