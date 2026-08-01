"""Recurring cleanup for retained artifact rows on gone Systems (ADR-0524, #1751)."""

from __future__ import annotations

import asyncio
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import SystemState
from kdive.reconciler.cleanup.gc import gc_system_artifacts
from tests.reconciler.conftest import connect, run_repair, seed_system


class _TwoPassStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        assert limit == 20
        self.calls.append(key)
        return len(self.calls) > 1


async def _insert_system_artifact(conn, system_id: UUID, key: str) -> UUID:
    row = await (
        await conn.execute(
            "INSERT INTO artifacts "
            "(owner_kind, owner_id, object_key, etag, sensitivity, retention_class) "
            "VALUES ('systems', %s, %s, 'etag', 'redacted', 'console') RETURNING id",
            (system_id, key),
        )
    ).fetchone()
    assert row is not None
    return row[0]


async def _artifact_exists(conn, artifact_id: UUID) -> bool:
    row = await (
        await conn.execute("SELECT 1 FROM artifacts WHERE id = %s", (artifact_id,))
    ).fetchone()
    return row is not None


def test_later_reconcile_pass_finishes_incomplete_system_artifact_retirement(
    migrated_url: str,
) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
            key = f"local/systems/{system_id}/console-part-0-0"
            artifact_id = await _insert_system_artifact(seed, system_id, key)
        store = _TwoPassStore()

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, lambda conn: gc_system_artifacts(conn, store)) == 0
            async with pool.connection() as check:
                assert await _artifact_exists(check, artifact_id)
            assert await run_repair(pool, lambda conn: gc_system_artifacts(conn, store)) == 1
            async with pool.connection() as check:
                assert not await _artifact_exists(check, artifact_id)

        assert store.calls == [key, key]

    asyncio.run(go())
