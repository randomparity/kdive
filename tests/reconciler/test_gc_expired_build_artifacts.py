"""gc_expired_build_artifacts TTL backstop for run-owned build artifacts (#768).

Reclaims ``owner_kind='runs'`` build artifacts older than the TTL regardless of investigation state.
Console (system-owned), build-log (run-owned evidence), and system-owned uploads are never touched.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg

from kdive.reconciler.cleanup.gc import gc_expired_build_artifacts
from tests.reconciler.conftest import connect


class _RecordingStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _seed_artifact(
    conn: psycopg.AsyncConnection,
    *,
    owner_kind: str,
    retention_class: str,
    age: timedelta,
) -> tuple[UUID, str]:
    artifact_id = uuid4()
    key = f"local/{owner_kind}/{artifact_id}"
    await conn.execute(
        "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class, created_at) VALUES (%s, %s, %s, %s, 'etag', 'redacted', %s, now() - %s)",
        (artifact_id, owner_kind, uuid4(), key, retention_class, age),
    )
    return artifact_id, key


async def _exists(conn: psycopg.AsyncConnection, artifact_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM artifacts WHERE id = %s", (artifact_id,))
    return await cur.fetchone() is not None


def test_reaps_only_old_run_build_artifacts(migrated_url: str) -> None:
    async def _run() -> None:
        old = timedelta(days=40)
        fresh = timedelta(days=1)
        seed = await connect(migrated_url)
        try:
            old_build, old_build_key = await _seed_artifact(
                seed, owner_kind="runs", retention_class="build", age=old
            )
            old_kbuild, old_kbuild_key = await _seed_artifact(
                seed, owner_kind="runs", retention_class="kernel-build", age=old
            )
            fresh_build, _ = await _seed_artifact(
                seed, owner_kind="runs", retention_class="build", age=fresh
            )
            old_log, _ = await _seed_artifact(
                seed, owner_kind="runs", retention_class="build-log", age=old
            )
            old_console, _ = await _seed_artifact(
                seed, owner_kind="systems", retention_class="console", age=old
            )
            old_system_build, _ = await _seed_artifact(
                seed, owner_kind="systems", retention_class="build", age=old
            )
        finally:
            await seed.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            deleted = await gc_expired_build_artifacts(conn, store, timedelta(days=30))
        finally:
            await conn.close()

        assert deleted == 2
        assert sorted(store.deleted) == sorted([old_build_key, old_kbuild_key])

        check = await connect(migrated_url)
        try:
            assert not await _exists(check, old_build)
            assert not await _exists(check, old_kbuild)
            assert await _exists(check, fresh_build)  # under TTL
            assert await _exists(check, old_log)  # run-owned evidence
            assert await _exists(check, old_console)  # system-owned crash evidence
            assert await _exists(check, old_system_build)  # operator base-image upload
        finally:
            await check.close()

    asyncio.run(_run())


def test_per_object_failure_isolated(migrated_url: str) -> None:
    async def _run() -> None:
        old = timedelta(days=40)
        seed = await connect(migrated_url)
        try:
            fail_id, fail_key = await _seed_artifact(
                seed, owner_kind="runs", retention_class="build", age=old
            )
            ok_id, ok_key = await _seed_artifact(
                seed, owner_kind="runs", retention_class="build", age=old
            )
        finally:
            await seed.close()

        class _FlakyStore:
            def __init__(self, bad: str) -> None:
                self.bad = bad
                self.deleted: list[str] = []

            def delete(self, key: str) -> None:
                if key == self.bad:
                    raise RuntimeError("object store unavailable")
                self.deleted.append(key)

        store = _FlakyStore(fail_key)
        conn = await connect(migrated_url)
        try:
            deleted = await gc_expired_build_artifacts(conn, store, timedelta(days=30))
        finally:
            await conn.close()

        assert deleted == 1
        assert store.deleted == [ok_key]
        check = await connect(migrated_url)
        try:
            assert await _exists(check, fail_id)  # kept for retry
            assert not await _exists(check, ok_id)
        finally:
            await check.close()

    asyncio.run(_run())
