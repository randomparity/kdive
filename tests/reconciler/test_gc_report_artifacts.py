"""gc_report_artifacts reaps only old report artifacts, object + row (ADR-0208)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg

from kdive.reconciler.cleanup.gc import gc_report_artifacts
from tests.reconciler.conftest import connect


class _RecordingStore:
    """Records deleted object keys; structurally an ArtifactObjectDeleter."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _insert_artifact(
    conn: psycopg.AsyncConnection, *, owner_kind: str, key: str, age: timedelta
) -> UUID:
    cur = await conn.execute(
        "INSERT INTO artifacts "
        "(owner_kind, owner_id, object_key, etag, sensitivity, retention_class, created_at) "
        "VALUES (%s, %s, %s, 'etag', 'redacted', 'report', now() - %s) RETURNING id",
        (owner_kind, uuid4(), key, age),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _exists(conn: psycopg.AsyncConnection, artifact_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM artifacts WHERE id = %s", (artifact_id,))
    return await cur.fetchone() is not None


def test_gc_deletes_only_old_report_artifacts(migrated_url: str) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            old_report = await _insert_artifact(
                seed, owner_kind="reports", key="local/reports/old.csv", age=timedelta(days=8)
            )
            fresh_report = await _insert_artifact(
                seed, owner_kind="reports", key="local/reports/fresh.csv", age=timedelta(hours=1)
            )
            old_system = await _insert_artifact(
                seed, owner_kind="systems", key="local/systems/core", age=timedelta(days=30)
            )
        finally:
            await seed.close()

        store = _RecordingStore()
        repair_conn = await connect(migrated_url)
        try:
            deleted = await gc_report_artifacts(repair_conn, store, timedelta(days=7))
        finally:
            await repair_conn.close()

        assert deleted == 1
        assert store.deleted == ["local/reports/old.csv"]

        check = await connect(migrated_url)
        try:
            assert not await _exists(check, old_report)  # reaped
            assert await _exists(check, fresh_report)  # under retention
            assert await _exists(check, old_system)  # different owner kind, untouched
        finally:
            await check.close()

    asyncio.run(_run())


def test_gc_per_object_failure_does_not_abort_sweep(migrated_url: str) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            a = await _insert_artifact(
                seed, owner_kind="reports", key="will-fail", age=timedelta(days=8)
            )
            b = await _insert_artifact(
                seed, owner_kind="reports", key="will-succeed", age=timedelta(days=8)
            )
        finally:
            await seed.close()

        class _FlakyStore:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def delete(self, key: str) -> None:
                if key == "will-fail":
                    raise RuntimeError("object store unavailable")
                self.deleted.append(key)

        store = _FlakyStore()
        repair_conn = await connect(migrated_url)
        try:
            deleted = await gc_report_artifacts(repair_conn, store, timedelta(days=7))
        finally:
            await repair_conn.close()

        assert deleted == 1
        assert store.deleted == ["will-succeed"]

        check = await connect(migrated_url)
        try:
            assert await _exists(check, a)  # row kept for retry next pass
            assert not await _exists(check, b)  # reaped
        finally:
            await check.close()

    asyncio.run(_run())
