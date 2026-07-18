"""Tests for shared artifact lookup queries."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg

from kdive.artifacts.read_model import raw_pcap_key, raw_vmcore_key


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _pcap_artifact(
    conn: psycopg.AsyncConnection,
    owner_id: UUID,
    object_key: str,
    *,
    created_at: str = "2026-01-01T00:00:00+00:00",
    owner_kind: str = "runs",
    retention_class: str = "pcap",
) -> UUID:
    row = await (
        await conn.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class, created_at) VALUES (%s, %s, %s, 'etag', 'sensitive', %s, %s) "
            "RETURNING id",
            (owner_kind, owner_id, object_key, retention_class, created_at),
        )
    ).fetchone()
    assert row is not None
    return row[0]


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


def test_raw_pcap_key_by_id_validates_ownership(migrated_url: str) -> None:
    async def _run() -> None:
        run_id = uuid4()
        other_run = uuid4()
        key = f"local/runs/{run_id}/pcap-job1"
        async with await _connect(migrated_url) as conn:
            aid = await _pcap_artifact(conn, run_id, key)
            other_aid = await _pcap_artifact(conn, other_run, f"local/runs/{other_run}/pcap-job2")

            assert await raw_pcap_key(conn, run_id, aid) == key
            # A pcap id owned by another Run does not resolve for this Run.
            assert await raw_pcap_key(conn, run_id, other_aid) is None
            # A missing id resolves to None.
            assert await raw_pcap_key(conn, run_id, uuid4()) is None

    asyncio.run(_run())


def test_raw_pcap_key_without_id_returns_newest(migrated_url: str) -> None:
    async def _run() -> None:
        run_id = uuid4()
        old_key = f"local/runs/{run_id}/pcap-old"
        new_key = f"local/runs/{run_id}/pcap-new"
        async with await _connect(migrated_url) as conn:
            await _pcap_artifact(conn, run_id, old_key, created_at="2026-01-01T00:00:00+00:00")
            await _pcap_artifact(conn, run_id, new_key, created_at="2026-06-01T00:00:00+00:00")

            assert await raw_pcap_key(conn, run_id, None) == new_key
            # A Run with no pcap resolves to None.
            assert await raw_pcap_key(conn, uuid4(), None) is None

    asyncio.run(_run())


def test_raw_pcap_key_ignores_non_pcap_retention(migrated_url: str) -> None:
    async def _run() -> None:
        run_id = uuid4()
        async with await _connect(migrated_url) as conn:
            # A vmcore-owned artifact of the same Run must not resolve as a pcap.
            vmcore_id = await _pcap_artifact(
                conn, run_id, f"local/runs/{run_id}/vmcore-kdump", retention_class="vmcore"
            )
            assert await raw_pcap_key(conn, run_id, vmcore_id) is None
            assert await raw_pcap_key(conn, run_id, None) is None

    asyncio.run(_run())
