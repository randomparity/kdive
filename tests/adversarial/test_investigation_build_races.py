"""Concurrency barriers for Investigation build generation reclamation (ADR-0531)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from kdive.db.locks import LockScope
from kdive.domain.capacity.state import InvestigationState
from kdive.mcp.tools.lifecycle.runs import steps as run_steps_module
from kdive.reconciler.cleanup import gc as gc_module
from kdive.reconciler.cleanup.gc import gc_expired_build_artifacts, gc_investigation_artifacts
from kdive.services.runs import admission as run_admission_module
from kdive.services.runs import complete_build as complete_build_module
from kdive.services.runs.complete_build import CompleteBuildFinalizer
from tests.mcp.complete_build_support import FakeValidator, seed_external_run_with_manifest
from tests.mcp.lifecycle.test_runs_tools import (
    _create,
    _ctx,
    _install,
    _pool,
    _seed_investigation,
    _seed_investigation_build,
    _seed_system,
)
from tests.reconciler.conftest import connect
from tests.services.runs.test_complete_build import (
    _complete,
    _output,
)


class _Store:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        return True

    def delete_version(self, key: str, version_id: str) -> None:
        self.deleted.append((key, version_id))


def test_real_create_wins_before_reclaim_lock(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquired = asyncio.Event()
    release = asyncio.Event()
    original_lock = run_admission_module.advisory_xact_lock

    @asynccontextmanager
    async def paused_lock(conn, scope, key):
        async with original_lock(conn, scope, key):
            if scope is LockScope.INVESTIGATION:
                acquired.set()
                await release.wait()
            yield

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            investigation_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            system_id = await _seed_system(pool)
            build_ref = await _seed_investigation_build(pool, investigation_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE investigations SET cleanup_pending_at = now() - interval '2 days' "
                    "WHERE id = %s",
                    (investigation_id,),
                )
            create = asyncio.create_task(
                _create(pool, _ctx(), investigation_id, system_id, build_ref=build_ref)
            )
            await acquired.wait()
            reclaimer = await connect(migrated_url)
            try:
                reclaim = asyncio.create_task(
                    gc_investigation_artifacts(reclaimer, _Store(), timedelta(days=1))
                )
                await asyncio.sleep(0)
                assert not reclaim.done()
                release.set()
                response = await create
                await reclaim
            finally:
                await reclaimer.close()
            assert response.status == "succeeded"

    with monkeypatch.context() as patched:
        patched.setattr(run_admission_module, "advisory_xact_lock", paused_lock)
        asyncio.run(_run())


def test_reclaim_lock_wins_and_real_create_rejects_reference(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquired = asyncio.Event()
    release = asyncio.Event()
    original_lock = gc_module.advisory_xact_lock

    @asynccontextmanager
    async def paused_lock(conn, scope, key):
        async with original_lock(conn, scope, key):
            acquired.set()
            await release.wait()
            yield

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            investigation_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            system_id = await _seed_system(pool)
            build_ref = await _seed_investigation_build(pool, investigation_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE investigation_builds SET expires_at = now() - interval '1 second' "
                    "WHERE investigation_id = %s AND build_ref = %s",
                    (investigation_id, build_ref),
                )
            reclaim_conn = await connect(migrated_url)
            try:
                reclaim = asyncio.create_task(
                    gc_expired_build_artifacts(reclaim_conn, _Store(), timedelta(days=30))
                )
                await acquired.wait()
                create = asyncio.create_task(
                    _create(
                        pool,
                        _ctx(),
                        investigation_id,
                        system_id,
                        build_ref=build_ref,
                    )
                )
                await asyncio.sleep(0)
                assert not create.done()
                release.set()
                await reclaim
                response = await create
            finally:
                await reclaim_conn.close()
            assert response.status == "error"
            assert response.data["reason"] == "build_ref_not_found"

    with monkeypatch.context() as patched:
        patched.setattr(gc_module, "advisory_xact_lock", paused_lock)
        asyncio.run(_run())


async def _seed_reusable_run(pool, *, expired: bool = True):
    investigation_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
    system_id = await _seed_system(pool)
    build_ref = await _seed_investigation_build(pool, investigation_id)
    response = await _create(pool, _ctx(), investigation_id, system_id, build_ref=build_ref)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE investigations SET cleanup_pending_at = now() - interval '2 days' "
            "WHERE id = %s",
            (investigation_id,),
        )
        if expired:
            await conn.execute(
                "UPDATE investigation_builds SET expires_at = now() - interval '1 second' "
                "WHERE investigation_id = %s AND build_ref = %s",
                (investigation_id, build_ref),
            )
    return investigation_id, build_ref, response.object_id


def test_reclaim_wins_and_real_install_rejects_reclaiming_reference(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquired = asyncio.Event()
    release = asyncio.Event()
    original_lock = gc_module.advisory_xact_lock

    @asynccontextmanager
    async def paused_lock(conn, scope, key):
        async with original_lock(conn, scope, key):
            acquired.set()
            await release.wait()
            yield

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _investigation_id, _build_ref, run_id = await _seed_reusable_run(pool, expired=False)
            reclaim_conn = await connect(migrated_url)
            try:
                reclaim = asyncio.create_task(
                    gc_investigation_artifacts(reclaim_conn, _Store(), timedelta(days=1))
                )
                await acquired.wait()
                install = asyncio.create_task(_install(pool, _ctx(), run_id))
                await asyncio.sleep(0)
                assert not install.done()
                release.set()
                await reclaim
                response = await install
            finally:
                await reclaim_conn.close()
            assert response.status == "error"
            assert response.data["reason"] == "build_ref_not_found"

    with monkeypatch.context() as patched:
        patched.setattr(gc_module, "advisory_xact_lock", paused_lock)
        asyncio.run(_run())


def test_real_install_wins_and_queued_job_pins_generation(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquired = asyncio.Event()
    release = asyncio.Event()
    original_lock = run_steps_module.advisory_xact_lock

    @asynccontextmanager
    async def paused_lock(conn, scope, key):
        async with original_lock(conn, scope, key):
            if scope is LockScope.INVESTIGATION:
                acquired.set()
                await release.wait()
            yield

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            investigation_id, build_ref, run_id = await _seed_reusable_run(pool, expired=False)
            install = asyncio.create_task(_install(pool, _ctx(), run_id))
            await acquired.wait()
            reclaim_conn = await connect(migrated_url)
            try:
                reclaim = asyncio.create_task(
                    gc_investigation_artifacts(reclaim_conn, _Store(), timedelta(days=1))
                )
                await asyncio.sleep(0)
                assert not reclaim.done()
                release.set()
                response = await install
                assert response.status == "queued"
                assert await reclaim == 0
                row = await (
                    await reclaim_conn.execute(
                        "SELECT state FROM investigation_builds "
                        "WHERE investigation_id = %s AND build_ref = %s",
                        (investigation_id, build_ref),
                    )
                ).fetchone()
                assert row == ("active",)
            finally:
                await reclaim_conn.close()

    with monkeypatch.context() as patched:
        patched.setattr(run_steps_module, "advisory_xact_lock", paused_lock)
        asyncio.run(_run())


def test_canceled_attempt_fence_acquired_after_selection_blocks_reclaim(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = asyncio.Event()
    release_selection = asyncio.Event()
    original_candidates = gc_module._generation_candidates

    async def paused_candidates(*args, **kwargs):
        rows = await original_candidates(*args, **kwargs)
        selected.set()
        await release_selection.wait()
        return rows

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            investigation_id, build_ref, run_id = await _seed_reusable_run(pool, expired=True)
            generation = build_ref.rsplit(".", 1)[1]
            object_key = f"builds/{generation}/kernel"
            job_id = uuid4()
            use_id = uuid4()
            async with pool.connection() as conn:
                await conn.execute("UPDATE runs SET state = 'succeeded' WHERE id = %s", (run_id,))
                await conn.execute(
                    "UPDATE investigation_builds SET artifacts = %s::jsonb "
                    "WHERE investigation_id = %s AND generation = %s",
                    (
                        Jsonb({"kernel": {"key": object_key, "version_id": "v-kernel"}}),
                        investigation_id,
                        generation,
                    ),
                )
                await conn.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                    "retention_class) VALUES ('investigations', %s, %s, 'etag', "
                    "'sensitive', 'build')",
                    (investigation_id, object_key),
                )
                await conn.execute(
                    "INSERT INTO jobs (id, kind, payload, state, max_attempts, attempt, "
                    "authorizing, dedup_key) VALUES (%s, 'install', %s::jsonb, 'canceled', "
                    "3, 1, '{}'::jsonb, %s)",
                    (job_id, Jsonb({"run_id": str(run_id)}), f"install-{run_id}"),
                )
            store = _Store()
            reclaim_conn = await connect(migrated_url)
            try:
                reclaim = asyncio.create_task(
                    gc_expired_build_artifacts(reclaim_conn, store, timedelta(days=30))
                )
                await selected.wait()
                async with pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO investigation_build_uses "
                        "(use_id, investigation_id, generation, job_id, attempt) "
                        "VALUES (%s, %s, %s, %s, 1)",
                        (use_id, investigation_id, generation, job_id),
                    )
                release_selection.set()
                assert await reclaim == 0
                assert store.deleted == []
                async with reclaim_conn.transaction():
                    await reclaim_conn.execute(
                        "DELETE FROM investigation_build_uses WHERE use_id = %s", (use_id,)
                    )
                assert await (
                    await reclaim_conn.execute(
                        "SELECT state, (SELECT count(*) FROM investigation_build_uses "
                        "WHERE investigation_id = %s AND generation = %s) "
                        "FROM investigation_builds WHERE investigation_id = %s AND generation = %s",
                        (investigation_id, generation, investigation_id, generation),
                    )
                ).fetchone() == ("active", 0)
                assert await (
                    await reclaim_conn.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
                ).fetchone() == ("succeeded",)
                assert await (
                    await reclaim_conn.execute(
                        "SELECT count(*) FROM jobs WHERE payload->>'run_id' = %s "
                        "AND kind = 'install' AND state IN ('queued', 'running')",
                        (str(run_id),),
                    )
                ).fetchone() == (0,)
                assert (
                    await gc_module._reclaim_generation(
                        reclaim_conn, store, investigation_id, UUID(generation)
                    )
                    > 0
                )
            finally:
                await reclaim_conn.close()

    with monkeypatch.context() as patched:
        patched.setattr(gc_module, "_generation_candidates", paused_candidates)
        asyncio.run(_run())


async def _seed_completion_race(pool):
    old_run_id = await seed_external_run_with_manifest(pool)
    new_run_id = await seed_external_run_with_manifest(pool)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET investigation_id = "
            "(SELECT investigation_id FROM runs WHERE id = %s) WHERE id = %s",
            (old_run_id, new_run_id),
        )
    old = await _complete(
        pool,
        old_run_id,
        CompleteBuildFinalizer(validate_complete_build=FakeValidator(_output(old_run_id))),
    )
    assert old.build_ref is not None
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT investigation_id FROM runs WHERE id = %s", (old_run_id,))
        ).fetchone()
        assert row is not None
        investigation_id = row[0]
        await conn.execute(
            "UPDATE investigation_builds SET expires_at = now() - interval '1 second' "
            "WHERE investigation_id = %s AND build_ref = %s",
            (investigation_id, old.build_ref),
        )
    return investigation_id, old, new_run_id


def test_real_complete_build_wins_and_reclaim_isolates_new_generation(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquired = asyncio.Event()
    release = asyncio.Event()
    pause_enabled = False
    original_lock = complete_build_module.advisory_xact_lock

    @asynccontextmanager
    async def paused_lock(conn, scope, key):
        async with original_lock(conn, scope, key):
            if pause_enabled and scope is LockScope.INVESTIGATION:
                acquired.set()
                await release.wait()
            yield

    async def _run() -> None:
        nonlocal pause_enabled
        async with _pool(migrated_url) as pool:
            investigation_id, old, new_run_id = await _seed_completion_race(pool)
            pause_enabled = True
            completion = asyncio.create_task(
                _complete(
                    pool,
                    new_run_id,
                    CompleteBuildFinalizer(
                        validate_complete_build=FakeValidator(_output(new_run_id))
                    ),
                )
            )
            await acquired.wait()
            reclaimer = await connect(migrated_url)
            try:
                reclaim = asyncio.create_task(
                    gc_expired_build_artifacts(reclaimer, _Store(), timedelta(days=30))
                )
                await asyncio.sleep(0)
                assert not reclaim.done()
                release.set()
                fresh = await completion
                assert await reclaim >= 1
            finally:
                await reclaimer.close()
            assert fresh.build_ref is not None and fresh.build_ref != old.build_ref
            async with pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT build_ref FROM investigation_builds WHERE investigation_id = %s",
                        (investigation_id,),
                    )
                ).fetchall()
            assert rows == [(fresh.build_ref,)]

    with monkeypatch.context() as patched:
        patched.setattr(complete_build_module, "advisory_xact_lock", paused_lock)
        asyncio.run(_run())


def test_reclaim_wins_and_real_complete_build_publishes_fresh_generation(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquired = asyncio.Event()
    release = asyncio.Event()
    original_lock = gc_module.advisory_xact_lock

    @asynccontextmanager
    async def paused_lock(conn, scope, key):
        async with original_lock(conn, scope, key):
            acquired.set()
            await release.wait()
            yield

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _investigation_id, old, new_run_id = await _seed_completion_race(pool)
            reclaimer = await connect(migrated_url)
            try:
                reclaim = asyncio.create_task(
                    gc_expired_build_artifacts(reclaimer, _Store(), timedelta(days=30))
                )
                await acquired.wait()
                completion = asyncio.create_task(
                    _complete(
                        pool,
                        new_run_id,
                        CompleteBuildFinalizer(
                            validate_complete_build=FakeValidator(_output(new_run_id))
                        ),
                    )
                )
                await asyncio.sleep(0)
                assert not completion.done()
                release.set()
                assert await reclaim >= 1
                fresh = await completion
            finally:
                await reclaimer.close()
            assert fresh.build_ref is not None and fresh.build_ref != old.build_ref

    with monkeypatch.context() as patched:
        patched.setattr(gc_module, "advisory_xact_lock", paused_lock)
        asyncio.run(_run())
