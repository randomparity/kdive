"""Idempotency-key retention repair for the reconciler."""

from __future__ import annotations

import logging
from datetime import timedelta

from psycopg import AsyncConnection

_log = logging.getLogger(__name__)

DEFAULT_IDEMPOTENCY_RETENTION = timedelta(days=7)


async def gc_idempotency_keys(conn: AsyncConnection, retention: timedelta) -> int:
    """Delete ``idempotency_keys`` rows older than ``retention`` (ADR-0040)."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM idempotency_keys WHERE created_at < now() - %s", (retention,)
        )
        deleted = cur.rowcount
    if deleted:
        _log.info("reconciler: GC'd %d idempotency key(s) past retention", deleted)
    return deleted
