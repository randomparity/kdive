"""MCP idempotency envelope adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _idempotency


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _success() -> ToolResponse:
    return ToolResponse.success(
        "run-1",
        "queued",
        suggested_next_actions=["jobs.get"],
        data={"job_id": "job-1"},
    )


def _failure() -> ToolResponse:
    return ToolResponse.failure(
        "run-1",
        ErrorCategory.CONFIGURATION_ERROR,
        detail="bad request",
        data={"reason": "bad_request"},
    )


def test_keyed_mutation_replays_stored_success_envelope(migrated_url: str) -> None:
    async def scenario() -> None:
        calls = 0

        async def work() -> ToolResponse:
            nonlocal calls
            calls += 1
            return _success()

        async with _pool(migrated_url) as pool, pool.connection() as conn:
            first = await _idempotency.keyed_mutation(
                conn,
                idempotency_key="same",
                principal="alice",
                project="proj",
                kind="runs.boot",
                do_work=work,
            )
            replay = await _idempotency.keyed_mutation(
                conn,
                idempotency_key="same",
                principal="alice",
                project="proj",
                kind="runs.boot",
                do_work=work,
            )
        assert calls == 1
        assert replay.model_dump() == first.model_dump()

    asyncio.run(scenario())


def test_keyed_mutation_does_not_store_failure_envelopes(migrated_url: str) -> None:
    async def scenario() -> None:
        calls = 0

        async def work() -> ToolResponse:
            nonlocal calls
            calls += 1
            return _failure()

        async with _pool(migrated_url) as pool, pool.connection() as conn:
            first = await _idempotency.keyed_mutation(
                conn,
                idempotency_key="retry",
                principal="alice",
                project="proj",
                kind="runs.boot",
                do_work=work,
            )
            second = await _idempotency.keyed_mutation(
                conn,
                idempotency_key="retry",
                principal="alice",
                project="proj",
                kind="runs.boot",
                do_work=work,
            )
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM idempotency_keys")
                row = await cur.fetchone()
        assert first.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert second.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert calls == 2
        assert row is not None and row["n"] == 0

    asyncio.run(scenario())


def test_keyed_mutation_maps_cross_kind_key_collision_to_conflict(migrated_url: str) -> None:
    async def scenario() -> None:
        async def work() -> ToolResponse:
            return _success()

        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await conn.execute(
                "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("shared", "alice", "proj", "systems.provision", Jsonb({"envelope": {}})),
            )
            resp = await _idempotency.keyed_mutation(
                conn,
                idempotency_key="shared",
                principal="alice",
                project="proj",
                kind="runs.boot",
                do_work=work,
            )
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.CONFLICT.value
        assert resp.data["reason"] == "idempotency_key_in_use"

    asyncio.run(scenario())
