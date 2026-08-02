"""gc_expired_build_artifacts TTL backstop for run-owned build artifacts (#768).

Reclaims ``owner_kind='runs'`` build artifacts older than the TTL regardless of investigation state.
Console (system-owned), build-log (run-owned evidence), and system-owned uploads are never touched.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, _lock_key
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup import gc as gc_module
from kdive.reconciler.cleanup.gc import gc_expired_build_artifacts
from kdive.services.runs.build_use import recover_build_use_after_confirmed_worker_death
from tests.reconciler.conftest import connect


class _RecordingStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        assert limit == 20
        self.deleted.append(key)
        return True

    def delete_version(self, key: str, version_id: str) -> None:
        self.deleted.append(f"{key}@{version_id}")


def test_generation_mark_commits_and_releases_lock_before_exact_delete(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        investigation_id, generation = uuid4(), uuid4()
        key = f"builds/{generation}/kernel"
        seed = await connect(migrated_url)
        try:
            await seed.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            digest = "d" * 64
            await seed.execute(
                "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                "content_digest, canonical_document, build_result, artifacts, target_kind, "
                "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, "
                "%s::jsonb, 'local-libvirt', '{}'::jsonb, now() - interval '1 second')",
                (
                    investigation_id,
                    generation,
                    f"{digest}.{generation}",
                    digest,
                    Jsonb({"kernel": {"key": key, "version_id": "v1"}}),
                ),
            )
        finally:
            await seed.close()

        class _ObserveThenFail(_RecordingStore):
            observed_state: tuple[str] | None = None
            observed_lock: tuple[bool] | None = None

            def delete_version(self, key: str, version_id: str) -> None:
                with psycopg.connect(migrated_url) as observer:
                    self.observed_state = observer.execute(
                        "SELECT state FROM investigation_builds WHERE generation = %s",
                        (generation,),
                    ).fetchone()
                    self.observed_lock = observer.execute(
                        "SELECT pg_try_advisory_xact_lock(%s)",
                        (_lock_key(LockScope.INVESTIGATION, investigation_id),),
                    ).fetchone()
                raise RuntimeError("fail after observing committed mark")

        store = _ObserveThenFail()
        pool = AsyncConnectionPool(migrated_url, open=False)
        await pool.open()
        try:
            async with pool.connection() as conn:
                assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0
        finally:
            await pool.close()
        assert store.observed_state == ("reclaiming",)
        assert store.observed_lock == (True,)
        check = await connect(migrated_url)
        try:
            row = await (
                await check.execute(
                    "SELECT state FROM investigation_builds WHERE generation = %s", (generation,)
                )
            ).fetchone()
            assert row == ("reclaiming",)
        finally:
            await check.close()

    asyncio.run(_run())


def test_expired_generation_reclaims_by_absolute_deadline(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        investigation_id = uuid4()
        generation = uuid4()
        job_id = uuid4()
        use_id = uuid4()
        digest = "b" * 64
        build_ref = f"{digest}.{generation}"
        key = f"builds/{generation}/kernel"
        try:
            await conn.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            await conn.execute(
                "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                "content_digest, canonical_document, build_result, artifacts, target_kind, "
                "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, "
                "%s::jsonb, 'local-libvirt', '{}'::jsonb, now() - interval '1 second')",
                (
                    investigation_id,
                    generation,
                    build_ref,
                    digest,
                    Jsonb({"kernel": {"key": key, "version_id": "v1"}}),
                ),
            )
            await conn.execute(
                "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                "retention_class) "
                "VALUES ('investigations', %s, %s, 'etag', 'sensitive', 'build')",
                (investigation_id, key),
            )
            await conn.execute(
                "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
                "lease_expires_at, authorizing, dedup_key) VALUES "
                "(%s, 'install', 'running', 1, 3, 'worker-1', now() + interval '5 min', "
                "'{}'::jsonb, %s)",
                (job_id, f"fence-{job_id}"),
            )
            await conn.execute(
                "INSERT INTO investigation_build_uses "
                "(use_id, investigation_id, generation, job_id, attempt, holder_worker_id, "
                "lease_expires_at) VALUES (%s, %s, %s, %s, 1, 'worker-1', "
                "now() + interval '5 min')",
                (use_id, investigation_id, generation, job_id),
            )
        finally:
            await conn.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0
            await conn.execute("DELETE FROM investigation_build_uses WHERE use_id = %s", (use_id,))
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 1
            result = await conn.execute(
                "SELECT 1 FROM investigation_builds WHERE generation = %s", (generation,)
            )
            assert await result.fetchone() is None
        finally:
            await conn.close()
        assert store.deleted == [f"{key}@v1"]

    asyncio.run(_run())


def test_overlapping_attempt_use_stays_pinned_until_each_handler_releases(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        investigation_id, generation, job_id = uuid4(), uuid4(), uuid4()
        digest = "e" * 64
        key = f"builds/{generation}/kernel"
        seed = await connect(migrated_url)
        try:
            await seed.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            await seed.execute(
                "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                "content_digest, canonical_document, build_result, artifacts, target_kind, "
                "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, "
                "%s::jsonb, 'local-libvirt', '{}'::jsonb, now() - interval '1 second')",
                (
                    investigation_id,
                    generation,
                    f"{digest}.{generation}",
                    digest,
                    Jsonb({"kernel": {"key": key, "version_id": "v1"}}),
                ),
            )
            await seed.execute(
                "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
                "lease_expires_at, authorizing, dedup_key) VALUES "
                "(%s, 'install', 'running', 1, 3, 'dead-worker', now() + interval '5 min', "
                "'{}'::jsonb, %s)",
                (job_id, f"use-{job_id}"),
            )
            old_use = uuid4()
            await seed.execute(
                "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, "
                "job_id, attempt, holder_worker_id, lease_expires_at) VALUES "
                "(%s, %s, %s, %s, 1, 'dead-worker', now() + interval '5 min')",
                (old_use, investigation_id, generation, job_id),
            )
        finally:
            await seed.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            # A reclaimed attempt cannot erase a still-live predecessor's overlap fence.
            await conn.execute(
                "UPDATE jobs SET attempt = 2, worker_id = 'new-worker', "
                "lease_expires_at = now() + interval '5 min' WHERE id = %s",
                (job_id,),
            )
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0

            # A lapsed heartbeat does not prove the first handler stopped (ADR-0018). A later
            # attempt therefore owns an independent fence without erasing its predecessor.
            await conn.execute(
                "UPDATE investigation_build_uses SET lease_expires_at = now() - interval '1 sec' "
                "WHERE use_id = %s",
                (old_use,),
            )
            new_use = uuid4()
            await conn.execute(
                "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, "
                "job_id, attempt, holder_worker_id, lease_expires_at) VALUES "
                "(%s, %s, %s, %s, 2, 'new-worker', now() + interval '5 min')",
                (new_use, investigation_id, generation, job_id),
            )
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0

            # Attempt 2 may complete while attempt 1 is still consuming the generation.
            await conn.execute("DELETE FROM investigation_build_uses WHERE use_id = %s", (new_use,))
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0

            # Recycling a terminal row back to attempt 1 does not collide with the still-live
            # predecessor. Worker identity plus use_id distinguish physical executions.
            await conn.execute(
                "UPDATE jobs SET attempt = 1, worker_id = 'recycled-worker' WHERE id = %s",
                (job_id,),
            )
            recycled_use = uuid4()
            await conn.execute(
                "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, "
                "job_id, attempt, holder_worker_id, lease_expires_at) VALUES "
                "(%s, %s, %s, %s, 1, 'recycled-worker', now() + interval '5 min')",
                (recycled_use, investigation_id, generation, job_id),
            )
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0
            with pytest.raises(ValueError, match="independent worker-death evidence"):
                await recover_build_use_after_confirmed_worker_death(
                    conn,
                    old_use,
                    confirmed_worker_id="dead-worker",
                    recovered_by="operator:test",
                    evidence=" ",
                )
            assert not await recover_build_use_after_confirmed_worker_death(
                conn,
                old_use,
                confirmed_worker_id="wrong-worker",
                recovered_by="operator:test",
                evidence="operator checked the wrong process",
            )
            assert await recover_build_use_after_confirmed_worker_death(
                conn,
                old_use,
                confirmed_worker_id="dead-worker",
                recovered_by="operator:test",
                evidence="operator confirmed host process exited",
            )
            assert await recover_build_use_after_confirmed_worker_death(
                conn,
                recycled_use,
                confirmed_worker_id="recycled-worker",
                recovered_by="operator:test",
                evidence="operator confirmed replacement process exited",
            )
            recoveries = await (
                await conn.execute(
                    "SELECT holder_worker_id, recovered_by, evidence "
                    "FROM investigation_build_use_recoveries ORDER BY holder_worker_id"
                )
            ).fetchall()
            assert recoveries == [
                (
                    "dead-worker",
                    "operator:test",
                    "operator confirmed host process exited",
                ),
                (
                    "recycled-worker",
                    "operator:test",
                    "operator confirmed replacement process exited",
                ),
            ]
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 1
        finally:
            await conn.close()
        assert store.deleted == [f"{key}@v1"]

    asyncio.run(_run())


def test_reclaiming_generation_retries_exact_versions_without_touching_fresh_generation(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        investigation_id = uuid4()
        old_generation = uuid4()
        fresh_generation = uuid4()
        digest = "c" * 64
        old_key = f"builds/{old_generation}/kernel"
        fresh_key = f"builds/{fresh_generation}/kernel"
        try:
            await conn.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            for generation, key, deadline in (
                (old_generation, old_key, "now() - interval '1 second'"),
                (fresh_generation, fresh_key, "now() + interval '1 day'"),
            ):
                await conn.execute(
                    "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                    "content_digest, canonical_document, build_result, artifacts, target_kind, "
                    "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, "
                    "'{}'::jsonb, %s::jsonb, 'local-libvirt', '{}'::jsonb, " + deadline + ")",
                    (
                        investigation_id,
                        generation,
                        f"{digest}.{generation}",
                        digest,
                        Jsonb({"kernel": {"key": key, "version_id": f"v-{generation}"}}),
                    ),
                )
                await conn.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                    "retention_class) VALUES "
                    "('investigations', %s, %s, 'etag', 'sensitive', 'build')",
                    (investigation_id, key),
                )
        finally:
            await conn.close()

        class _FailOnceStore(_RecordingStore):
            failed = False

            def delete_version(self, key: str, version_id: str) -> None:
                self.deleted.append(f"{key}@{version_id}")
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("temporary store failure")

        store = _FailOnceStore()
        conn = await connect(migrated_url)
        try:
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0
            state = await (
                await conn.execute(
                    "SELECT state FROM investigation_builds WHERE generation = %s",
                    (old_generation,),
                )
            ).fetchone()
            assert state == ("reclaiming",)
            await conn.execute(
                "UPDATE investigation_builds SET reclaim_retry_at = now() - interval '1 second' "
                "WHERE generation = %s",
                (old_generation,),
            )
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 1
            remaining = await (
                await conn.execute(
                    "SELECT generation, state FROM investigation_builds ORDER BY generation"
                )
            ).fetchall()
            assert remaining == [(fresh_generation, "active")]
            keys = await (
                await conn.execute(
                    "SELECT object_key FROM artifacts WHERE owner_id = %s", (investigation_id,)
                )
            ).fetchall()
            assert keys == [(fresh_key,)]
        finally:
            await conn.close()
        assert all(not entry.startswith(fresh_key) for entry in store.deleted)

    asyncio.run(_run())


def test_generation_reclaim_pass_is_ordered_bounded_and_resumes(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        investigation_id = uuid4()
        generations = [UUID(int=value) for value in (3, 1, 2)]
        try:
            await conn.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            for generation in generations:
                digest = f"{generation.int:064x}"
                key = f"builds/{generation}/kernel"
                await conn.execute(
                    "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                    "content_digest, canonical_document, build_result, artifacts, target_kind, "
                    "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, "
                    "'{}'::jsonb, %s::jsonb, 'local-libvirt', '{}'::jsonb, "
                    "now() - interval '1 second')",
                    (
                        investigation_id,
                        generation,
                        f"{digest}.{generation}",
                        digest,
                        Jsonb({"kernel": {"key": key, "version_id": f"v-{generation}"}}),
                    ),
                )
        finally:
            await conn.close()

        monkeypatch.setattr(gc_module, "_BUILD_GENERATIONS_PER_PASS", 2)
        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 2
            remaining = await (
                await conn.execute(
                    "SELECT generation FROM investigation_builds ORDER BY generation"
                )
            ).fetchall()
            assert remaining == [(UUID(int=3),)]
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 1
        finally:
            await conn.close()
        assert store.deleted == [
            f"builds/{UUID(int=1)}/kernel@v-{UUID(int=1)}",
            f"builds/{UUID(int=2)}/kernel@v-{UUID(int=2)}",
            f"builds/{UUID(int=3)}/kernel@v-{UUID(int=3)}",
        ]

    asyncio.run(_run())


def test_pinned_generation_does_not_consume_reclaim_budget(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        investigation_id = uuid4()
        pinned_generation = UUID(int=1)
        eligible_generation = UUID(int=2)
        try:
            await conn.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            for generation in (pinned_generation, eligible_generation):
                digest = f"{generation.int:064x}"
                await conn.execute(
                    "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                    "content_digest, canonical_document, build_result, artifacts, target_kind, "
                    "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, "
                    "'{}'::jsonb, %s::jsonb, 'local-libvirt', '{}'::jsonb, "
                    "now() - interval '1 second')",
                    (
                        investigation_id,
                        generation,
                        f"{digest}.{generation}",
                        digest,
                        Jsonb(
                            {
                                "kernel": {
                                    "key": f"builds/{generation}/kernel",
                                    "version_id": f"v-{generation}",
                                }
                            }
                        ),
                    ),
                )
            await conn.execute(
                "INSERT INTO runs (id, investigation_id, state, build_profile, target_kind, "
                "principal, project, build_ref) VALUES (%s, %s, 'created', '{}'::jsonb, "
                "'local-libvirt', 'p', 'proj', %s)",
                (uuid4(), investigation_id, f"{pinned_generation.int:064x}.{pinned_generation}"),
            )
        finally:
            await conn.close()

        monkeypatch.setattr(gc_module, "_BUILD_GENERATIONS_PER_PASS", 1)
        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 1
            rows = await (
                await conn.execute(
                    "SELECT generation FROM investigation_builds ORDER BY generation"
                )
            ).fetchall()
            assert rows == [(pinned_generation,)]
        finally:
            await conn.close()

    asyncio.run(_run())


def test_failed_generation_backs_off_without_starving_later_tenant(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        first_id, later_id = UUID(int=1), UUID(int=2)
        first_generation, later_generation = UUID(int=11), UUID(int=22)
        try:
            for investigation_id, generation in (
                (first_id, first_generation),
                (later_id, later_generation),
            ):
                digest = f"{generation.int:064x}"
                key = f"builds/{generation}/kernel"
                await conn.execute(
                    "INSERT INTO investigations (id, principal, project, title, state) "
                    "VALUES (%s, 'p', 'proj', 't', 'active')",
                    (investigation_id,),
                )
                await conn.execute(
                    "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                    "content_digest, canonical_document, build_result, artifacts, target_kind, "
                    "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, "
                    "'{}'::jsonb, %s::jsonb, 'local-libvirt', '{}'::jsonb, now() - interval '1s')",
                    (
                        investigation_id,
                        generation,
                        f"{digest}.{generation}",
                        digest,
                        Jsonb({"kernel": {"key": key, "version_id": f"v-{generation}"}}),
                    ),
                )
        finally:
            await conn.close()

        class _FirstTenantFails(_RecordingStore):
            def delete_version(self, key: str, version_id: str) -> None:
                self.deleted.append(f"{key}@{version_id}")
                if str(first_generation) in key:
                    raise RuntimeError("permanent first-tenant fault")

        monkeypatch.setattr(gc_module, "_BUILD_GENERATIONS_PER_PASS", 1)
        store = _FirstTenantFails()
        conn = await connect(migrated_url)
        try:
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 1
            retry = await (
                await conn.execute(
                    "SELECT reclaim_retry_at > now() FROM investigation_builds "
                    "WHERE generation = %s",
                    (first_generation,),
                )
            ).fetchone()
            assert retry == (True,)
        finally:
            await conn.close()
        assert any(str(later_generation) in deleted for deleted in store.deleted)

    asyncio.run(_run())


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

            def delete_retired_key_batch(self, key: str, limit: int) -> bool:
                assert limit == 20
                if key == self.bad:
                    raise RuntimeError("object store unavailable")
                self.deleted.append(key)
                return True

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


def test_gc_retains_expired_build_row_until_retired_key_batch_completes(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            artifact_id, key = await _seed_artifact(
                seed,
                owner_kind="runs",
                retention_class="build",
                age=timedelta(days=40),
            )
        finally:
            await seed.close()

        class _SequencedStore:
            def __init__(self) -> None:
                self.outcomes: list[bool | Exception] = [
                    False,
                    CategorizedError(
                        "delete failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE
                    ),
                    True,
                ]
                self.calls: list[tuple[str, int]] = []

            def delete_retired_key_batch(self, key: str, limit: int) -> bool:
                self.calls.append((key, limit))
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        store = _SequencedStore()
        conn = await connect(migrated_url)
        try:
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0
            assert await _exists(conn, artifact_id)
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 0
            assert await _exists(conn, artifact_id)
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 1
            assert not await _exists(conn, artifact_id)
        finally:
            await conn.close()

        assert store.calls == [(key, 20)] * 3

    asyncio.run(_run())
