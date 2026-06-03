"""Connection-scoped operations over the durable ``jobs`` queue (ADR-0018, issue #9).

``enqueue`` admits a job idempotently on ``dedup_key``; ``dequeue`` claims the oldest
eligible job with ``FOR UPDATE SKIP LOCKED``, charging an attempt and reclaiming a
lapsed lease; ``heartbeat`` renews a lease; ``complete`` and ``fail`` finalize a
claimed job. Every post-claim write is fenced on ``worker_id`` + ``state = 'running'``
so a worker that lost its lease cannot mutate a job another worker now owns. Each
function wraps its statements in ``conn.transaction()`` so it self-commits on any
connection, and all assume READ COMMITTED (psycopg's default).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.domain.models import Job, JobKind

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE = timedelta(minutes=5)


async def enqueue(
    conn: AsyncConnection,
    kind: JobKind,
    payload: dict[str, Any],
    authorizing: dict[str, Any],
    dedup_key: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Job:
    """Admit a job, returning the existing one on a ``dedup_key`` conflict.

    Upsert-then-fetch: ``INSERT … ON CONFLICT (dedup_key) DO NOTHING`` then
    ``SELECT … WHERE dedup_key = …`` in one transaction, so a re-issue returns the
    **same** job (in whatever state it has since reached) and never enqueues a
    duplicate. ``DO NOTHING RETURNING`` is avoided — it returns no row on conflict.

    Raises:
        ValueError: ``max_attempts < 1`` (a job that ``dequeue`` could never claim).
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
            "VALUES (%s, %s, 'queued', %s, %s, %s) "
            "ON CONFLICT (dedup_key) DO NOTHING",
            (kind, Jsonb(payload), max_attempts, Jsonb(authorizing), dedup_key),
        )
        await cur.execute("SELECT * FROM jobs WHERE dedup_key = %s", (dedup_key,))
        row = await cur.fetchone()
    if row is None:  # Invariant: we just inserted the row, or it already existed.
        raise RuntimeError(f"enqueue found no job for dedup_key {dedup_key!r}")
    return Job.model_validate(row)
