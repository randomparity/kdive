"""gc_investigation_artifacts reclaims run-owned build artifacts of closed investigations (#768).

Scoped to ``owner_kind='runs'`` + a build ``retention_class`` linked via ``runs.investigation_id``
to an investigation marked ``cleanup_pending_at`` past the grace window. Console (system-owned) and
build-log (run-owned evidence) are never touched; the marker is cleared after a full drain.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg

from kdive.reconciler.cleanup.gc import gc_investigation_artifacts
from tests.reconciler.conftest import connect


class _RecordingStore:
    """Records deleted object keys; structurally an ArtifactObjectDeleter."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _seed_investigation(
    conn: psycopg.AsyncConnection, *, state: str, marker_age: timedelta | None
) -> UUID:
    inv_id = uuid4()
    if marker_age is None:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state, cleanup_pending_at) "
            "VALUES (%s, 'p', 'proj', 't', %s, NULL)",
            (inv_id, state),
        )
    else:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state, cleanup_pending_at) "
            "VALUES (%s, 'p', 'proj', 't', %s, now() - %s)",
            (inv_id, state, marker_age),
        )
    return inv_id


async def _seed_run(conn: psycopg.AsyncConnection, investigation_id: UUID) -> UUID:
    run_id = uuid4()
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, state, build_profile, target_kind, "
        "principal, project) "
        "VALUES (%s, %s, NULL, 'created', '{}'::jsonb, 'local-libvirt', 'p', 'proj')",
        (run_id, investigation_id),
    )
    return run_id


async def _seed_artifact(
    conn: psycopg.AsyncConnection, *, owner_kind: str, owner_id: UUID, retention_class: str
) -> tuple[UUID, str]:
    artifact_id = uuid4()
    key = f"local/{owner_kind}/{artifact_id}"
    await conn.execute(
        "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES (%s, %s, %s, %s, 'etag', 'redacted', %s)",
        (artifact_id, owner_kind, owner_id, key, retention_class),
    )
    return artifact_id, key


async def _exists(conn: psycopg.AsyncConnection, artifact_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM artifacts WHERE id = %s", (artifact_id,))
    return await cur.fetchone() is not None


async def _marker(conn: psycopg.AsyncConnection, inv_id: UUID) -> object:
    cur = await conn.execute(
        "SELECT cleanup_pending_at FROM investigations WHERE id = %s", (inv_id,)
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


def test_reclaims_only_run_build_artifacts_of_closed_past_grace(migrated_url: str) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", marker_age=timedelta(days=2))
            run = await _seed_run(seed, inv)
            build, build_key = await _seed_artifact(
                seed, owner_kind="runs", owner_id=run, retention_class="build"
            )
            kbuild, kbuild_key = await _seed_artifact(
                seed, owner_kind="runs", owner_id=run, retention_class="kernel-build"
            )
            build_log, _ = await _seed_artifact(
                seed, owner_kind="runs", owner_id=run, retention_class="build-log"
            )
            console, _ = await _seed_artifact(
                seed, owner_kind="systems", owner_id=uuid4(), retention_class="console"
            )
        finally:
            await seed.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            deleted = await gc_investigation_artifacts(conn, store, timedelta(days=1))
        finally:
            await conn.close()

        assert deleted == 2
        assert sorted(store.deleted) == sorted([build_key, kbuild_key])

        check = await connect(migrated_url)
        try:
            assert not await _exists(check, build)
            assert not await _exists(check, kbuild)
            assert await _exists(check, build_log)  # run-owned evidence, excluded
            assert await _exists(check, console)  # system-owned crash evidence, excluded
            assert await _marker(check, inv) is None  # cleared after full drain
        finally:
            await check.close()

    asyncio.run(_run())


def test_under_grace_is_untouched_and_marker_retained(migrated_url: str) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", marker_age=timedelta(hours=1))
            run = await _seed_run(seed, inv)
            build, _ = await _seed_artifact(
                seed, owner_kind="runs", owner_id=run, retention_class="build"
            )
        finally:
            await seed.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            deleted = await gc_investigation_artifacts(conn, store, timedelta(days=1))
        finally:
            await conn.close()

        assert deleted == 0
        check = await connect(migrated_url)
        try:
            assert await _exists(check, build)
            assert await _marker(check, inv) is not None
        finally:
            await check.close()

    asyncio.run(_run())


def test_open_investigation_is_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="open", marker_age=None)
            run = await _seed_run(seed, inv)
            build, _ = await _seed_artifact(
                seed, owner_kind="runs", owner_id=run, retention_class="build"
            )
        finally:
            await seed.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            deleted = await gc_investigation_artifacts(conn, store, timedelta(days=1))
        finally:
            await conn.close()

        assert deleted == 0
        check = await connect(migrated_url)
        try:
            assert await _exists(check, build)
        finally:
            await check.close()

    asyncio.run(_run())


def test_per_object_failure_keeps_row_and_marker(migrated_url: str) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", marker_age=timedelta(days=2))
            run = await _seed_run(seed, inv)
            fail_id, fail_key = await _seed_artifact(
                seed, owner_kind="runs", owner_id=run, retention_class="build"
            )
            ok_id, ok_key = await _seed_artifact(
                seed, owner_kind="runs", owner_id=run, retention_class="kernel-build"
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
            deleted = await gc_investigation_artifacts(conn, store, timedelta(days=1))
        finally:
            await conn.close()

        assert deleted == 1
        assert store.deleted == [ok_key]
        check = await connect(migrated_url)
        try:
            assert await _exists(check, fail_id)  # kept for retry
            assert not await _exists(check, ok_id)
            assert await _marker(check, inv) is not None  # marker retained on partial failure
        finally:
            await check.close()

    asyncio.run(_run())


def test_idempotent_after_full_drain(migrated_url: str) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", marker_age=timedelta(days=2))
            run = await _seed_run(seed, inv)
            await _seed_artifact(seed, owner_kind="runs", owner_id=run, retention_class="build")
        finally:
            await seed.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            first = await gc_investigation_artifacts(conn, store, timedelta(days=1))
            second = await gc_investigation_artifacts(conn, store, timedelta(days=1))
        finally:
            await conn.close()

        assert first == 1
        assert second == 0  # marker cleared, nothing left to do

    asyncio.run(_run())
