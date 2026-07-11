"""Tests for shared artifact lookup queries."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg

from kdive.artifacts.read_model import raw_vmcore_key


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _artifact(
    conn: psycopg.AsyncConnection,
    owner_id: UUID,
    object_key: str,
    *,
    owner_kind: str = "runs",
    sensitivity: str = "sensitive",
) -> None:
    await conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES (%s, %s, %s, 'etag', %s, 'vmcore')",
        (owner_kind, owner_id, object_key, sensitivity),
    )


def test_raw_vmcore_key_returns_only_matching_unredacted_run_key(migrated_url: str) -> None:
    async def _run() -> None:
        run_id = uuid4()
        other_run_id = uuid4()
        raw_key = f"local/runs/{run_id}/vmcore-host_dump"
        async with await _connect(migrated_url) as conn:
            await _artifact(conn, run_id, raw_key)
            await _artifact(conn, run_id, f"{raw_key}-redacted", sensitivity="redacted")
            await _artifact(conn, other_run_id, f"local/runs/{other_run_id}/vmcore-kdump")

            assert await raw_vmcore_key(conn, run_id) == raw_key
            assert await raw_vmcore_key(conn, uuid4()) is None

    asyncio.run(_run())


def test_raw_vmcore_key_ignores_system_owned_core(migrated_url: str) -> None:
    """A core owned by a System (the pre-ADR-0244 shape) is not resolved by run id."""

    async def _run() -> None:
        ident = uuid4()
        async with await _connect(migrated_url) as conn:
            await _artifact(
                conn,
                ident,
                f"local/systems/{ident}/vmcore-host_dump",
                owner_kind="systems",
            )
            assert await raw_vmcore_key(conn, ident) is None

    asyncio.run(_run())
