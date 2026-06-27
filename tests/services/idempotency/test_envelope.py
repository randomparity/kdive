"""Tests for the generalized envelope-replay idempotency helper (ADR-0193)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.services.idempotency.envelope import (
    StoredResult,
    record_result,
    resolve_conflict,
    resolve_replay,
    validate_idempotency_key,
)


@asynccontextmanager
async def _open_pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _result() -> StoredResult:
    return StoredResult(
        {
            "object_id": "11111111-1111-1111-1111-111111111111",
            "status": "created",
            "suggested_next_actions": ["runs.get"],
            "refs": [],
            "error_category": None,
            "detail": None,
            "data": {"project": "proj", "target_kind": "local-libvirt"},
        }
    )


def test_validate_idempotency_key_bounds() -> None:
    validate_idempotency_key("k")
    validate_idempotency_key("x" * 200)
    for bad in ("", "x" * 201):
        with pytest.raises(CategorizedError) as exc:
            validate_idempotency_key(bad)
        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_record_then_resolve_returns_identical_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _open_pool(migrated_url) as pool, pool.connection() as conn:
            result = _result()
            async with conn.transaction():
                await record_result(
                    conn,
                    principal="alice",
                    key="k1",
                    project="proj",
                    kind="runs.create",
                    result=result,
                )
            got = await resolve_replay(conn, principal="alice", key="k1", kind="runs.create")
            assert got is not None
            assert got.document == result.document

    asyncio.run(_run())


def test_resolve_miss_returns_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with _open_pool(migrated_url) as pool, pool.connection() as conn:
            async with conn.transaction():
                await record_result(
                    conn,
                    principal="alice",
                    key="k1",
                    project="proj",
                    kind="runs.create",
                    result=_result(),
                )
            # Different principal, key, and kind each miss.
            assert await resolve_replay(conn, principal="bob", key="k1", kind="runs.create") is None
            assert (
                await resolve_replay(conn, principal="alice", key="k2", kind="runs.create") is None
            )
            assert (
                await resolve_replay(conn, principal="alice", key="k1", kind="systems.provision")
                is None
            )

    asyncio.run(_run())


def test_duplicate_record_raises_unique_violation_then_resolve_conflict_replays(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _open_pool(migrated_url) as pool, pool.connection() as conn:
            result = _result()
            async with conn.transaction():
                await record_result(
                    conn,
                    principal="alice",
                    key="k1",
                    project="proj",
                    kind="runs.create",
                    result=result,
                )
            # A second insert under the same (principal, key) aborts; resolve_conflict under
            # the same kind replays the first envelope (the self-race path).
            with pytest.raises(UniqueViolation):
                async with conn.transaction():
                    await record_result(
                        conn,
                        principal="alice",
                        key="k1",
                        project="proj",
                        kind="runs.create",
                        result=result,
                    )
            replay = await resolve_conflict(conn, principal="alice", key="k1", kind="runs.create")
            assert replay.document == result.document

    asyncio.run(_run())


def test_resolve_conflict_cross_tool_raises_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _open_pool(migrated_url) as pool, pool.connection() as conn:
            async with conn.transaction():
                await record_result(
                    conn,
                    principal="alice",
                    key="shared",
                    project="proj",
                    kind="runs.create",
                    result=_result(),
                )
            # The same (principal, key) collides on the PK, but resolve_conflict under a
            # *different* tool's kind finds no matching row -> genuine cross-tool misuse.
            with pytest.raises(CategorizedError) as exc:
                await resolve_conflict(
                    conn, principal="alice", key="shared", kind="systems.provision"
                )
            assert exc.value.category is ErrorCategory.CONFLICT

    asyncio.run(_run())
