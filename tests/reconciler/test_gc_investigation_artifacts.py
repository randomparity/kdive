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
import pytest
from psycopg.types.json import Jsonb

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.gc import gc_investigation_artifacts
from tests.reconciler.conftest import connect


class _RecordingStore:
    """Records deleted object keys; structurally an ArtifactObjectDeleter."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        assert limit == 20
        self.deleted.append(key)
        return True

    def delete_version(self, key: str, version_id: str) -> None:
        self.deleted.append(f"{key}@{version_id}")


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


async def _seed_generation(
    conn: psycopg.AsyncConnection, investigation_id: UUID, *, expired: bool = False
) -> tuple[UUID, str, list[tuple[str, str]]]:
    generation = uuid4()
    digest = f"{generation.int:064x}"
    build_ref = f"{digest}.{generation}"
    versions = [
        (f"builds/{generation}/kernel", "v-kernel"),
        (f"builds/{generation}/debug", "v-debug"),
    ]
    artifacts = {
        "kernel": {"key": versions[0][0], "version_id": versions[0][1]},
        "debuginfo": {"key": versions[1][0], "version_id": versions[1][1]},
    }
    await conn.execute(
        "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
        "content_digest, canonical_document, build_result, artifacts, target_kind, "
        "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, "
        "%s::jsonb, 'local-libvirt', '{}'::jsonb, "
        "CASE WHEN %s THEN now() - interval '1 second' ELSE now() + interval '1 day' END)",
        (
            investigation_id,
            generation,
            build_ref,
            digest,
            Jsonb(artifacts),
            expired,
        ),
    )
    for key, _version in versions:
        await conn.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('investigations', %s, %s, 'etag', 'sensitive', 'build')",
            (investigation_id, key),
        )
    return generation, build_ref, versions


def test_closed_generation_reclaims_only_its_exact_versions(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        try:
            investigation_id = await _seed_investigation(
                conn, state="closed", marker_age=timedelta(days=2)
            )
            generation, _build_ref, versions = await _seed_generation(conn, investigation_id)
        finally:
            await conn.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 2
            row = await (
                await conn.execute(
                    "SELECT state FROM investigation_builds WHERE investigation_id = %s "
                    "AND generation = %s",
                    (investigation_id, generation),
                )
            ).fetchone()
            assert row is None
            assert await _marker(conn, investigation_id) is None
        finally:
            await conn.close()

        assert sorted(store.deleted) == sorted(f"{key}@{version}" for key, version in versions)

    asyncio.run(_run())


def test_closed_generation_is_pinned_by_queued_install(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        try:
            investigation_id = await _seed_investigation(
                conn, state="closed", marker_age=timedelta(days=2)
            )
            generation, build_ref, _versions = await _seed_generation(conn, investigation_id)
            run_id = await _seed_run(conn, investigation_id)
            await conn.execute(
                "UPDATE runs SET state = 'succeeded', build_ref = %s WHERE id = %s",
                (build_ref, run_id),
            )
            await conn.execute(
                "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
                "VALUES ('install', %s::jsonb, 'queued', 3, '{}'::jsonb, %s)",
                (Jsonb({"run_id": str(run_id)}), f"install-{run_id}"),
            )
        finally:
            await conn.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 0
            row = await (
                await conn.execute(
                    "SELECT state FROM investigation_builds WHERE investigation_id = %s "
                    "AND generation = %s",
                    (investigation_id, generation),
                )
            ).fetchone()
            assert row == ("active",)
            assert await _marker(conn, investigation_id) is not None
        finally:
            await conn.close()
        assert store.deleted == []

    asyncio.run(_run())


def test_closed_generation_is_pinned_by_non_terminal_run(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        try:
            investigation_id = await _seed_investigation(
                conn, state="closed", marker_age=timedelta(days=2)
            )
            generation, build_ref, _versions = await _seed_generation(conn, investigation_id)
            run_id = await _seed_run(conn, investigation_id)
            await conn.execute("UPDATE runs SET build_ref = %s WHERE id = %s", (build_ref, run_id))
        finally:
            await conn.close()

        conn = await connect(migrated_url)
        try:
            assert await gc_investigation_artifacts(conn, _RecordingStore(), timedelta(days=1)) == 0
            state = await (
                await conn.execute(
                    "SELECT state FROM investigation_builds WHERE generation = %s", (generation,)
                )
            ).fetchone()
            assert state == ("active",)
        finally:
            await conn.close()

    asyncio.run(_run())


def test_settled_install_releases_generation_pin(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        try:
            investigation_id = await _seed_investigation(
                conn, state="closed", marker_age=timedelta(days=2)
            )
            _generation, build_ref, versions = await _seed_generation(conn, investigation_id)
            run_id = await _seed_run(conn, investigation_id)
            await conn.execute(
                "UPDATE runs SET state = 'succeeded', build_ref = %s WHERE id = %s",
                (build_ref, run_id),
            )
            await conn.execute(
                "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
                "VALUES ('install', %s::jsonb, 'failed', 3, '{}'::jsonb, %s)",
                (Jsonb({"run_id": str(run_id)}), f"install-{run_id}"),
            )
        finally:
            await conn.close()

        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 2
        finally:
            await conn.close()
        assert sorted(store.deleted) == sorted(f"{key}@{version}" for key, version in versions)

    asyncio.run(_run())


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

            def delete_retired_key_batch(self, key: str, limit: int) -> bool:
                assert limit == 20
                if key == self.bad:
                    raise RuntimeError("object store unavailable")
                self.deleted.append(key)
                return True

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


def test_gc_retains_investigation_marker_until_retired_key_batch_completes(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            investigation_id = await _seed_investigation(
                seed, state="closed", marker_age=timedelta(days=2)
            )
            run_id = await _seed_run(seed, investigation_id)
            artifact_id, key = await _seed_artifact(
                seed, owner_kind="runs", owner_id=run_id, retention_class="build"
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
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 0
            assert await _exists(conn, artifact_id)
            assert await _marker(conn, investigation_id) is not None
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 0
            assert await _exists(conn, artifact_id)
            assert await _marker(conn, investigation_id) is not None
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 1
            assert not await _exists(conn, artifact_id)
            assert await _marker(conn, investigation_id) is None
        finally:
            await conn.close()

        assert store.calls == [(key, 20)] * 3

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


def test_close_driven_generation_budget_is_fair_across_investigations(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.reconciler.cleanup import gc as gc_module

    async def _run() -> None:
        conn = await connect(migrated_url)
        first_id, later_id = UUID(int=1), UUID(int=2)
        try:
            for investigation_id in (first_id, later_id):
                await conn.execute(
                    "INSERT INTO investigations "
                    "(id, principal, project, title, state, cleanup_pending_at) "
                    "VALUES (%s, 'p', 'proj', 't', 'closed', now() - interval '2 days')",
                    (investigation_id,),
                )
            for _ in range(3):
                await _seed_generation(conn, first_id)
            later_generation, _ref, _versions = await _seed_generation(conn, later_id)
        finally:
            await conn.close()

        class _FirstFails(_RecordingStore):
            def delete_version(self, key: str, version_id: str) -> None:
                self.deleted.append(f"{key}@{version_id}")
                if str(later_generation) not in key:
                    raise RuntimeError("early tenant failure")

        monkeypatch.setattr(gc_module, "_BUILD_GENERATIONS_PER_PASS", 2)
        store = _FirstFails()
        conn = await connect(migrated_url)
        try:
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 2
        finally:
            await conn.close()
        assert any(str(later_generation) in call for call in store.deleted)

    asyncio.run(_run())


def test_public_close_gc_bounds_legacy_calls_and_advances_fairly(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.reconciler.cleanup import gc as gc_module

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            investigation_id = await _seed_investigation(
                seed, state="closed", marker_age=timedelta(days=2)
            )
            run_id = await _seed_run(seed, investigation_id)
            keys = [
                (
                    await _seed_artifact(
                        seed, owner_kind="runs", owner_id=run_id, retention_class="build"
                    )
                )[1]
                for _ in range(5)
            ]
        finally:
            await seed.close()

        monkeypatch.setattr(gc_module, "_LEGACY_BUILD_ARTIFACTS_PER_PASS", 2)
        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 2
            assert len(store.deleted) == 2
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 2
            assert len(store.deleted) == 4
            assert await gc_investigation_artifacts(conn, store, timedelta(days=1)) == 1
            assert set(store.deleted) == set(keys)
        finally:
            await conn.close()

    asyncio.run(_run())


def test_public_close_gc_bounds_marker_cleanup_and_resumes_from_cursor(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.reconciler.cleanup import gc as gc_module

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            investigation_ids = [
                await _seed_investigation(seed, state="closed", marker_age=timedelta(days=2))
                for _ in range(5)
            ]
        finally:
            await seed.close()

        monkeypatch.setattr(gc_module, "_CLOSED_INVESTIGATIONS_PER_PASS", 2)
        conn = await connect(migrated_url)
        try:
            await gc_investigation_artifacts(conn, _RecordingStore(), timedelta(days=1))
            assert sum([await _marker(conn, item) is None for item in investigation_ids]) == 2
            await gc_investigation_artifacts(conn, _RecordingStore(), timedelta(days=1))
            assert sum([await _marker(conn, item) is None for item in investigation_ids]) == 4
            await gc_investigation_artifacts(conn, _RecordingStore(), timedelta(days=1))
            assert all([await _marker(conn, item) is None for item in investigation_ids])
        finally:
            await conn.close()

    asyncio.run(_run())
