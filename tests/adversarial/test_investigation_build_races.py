"""Concurrency barriers for Investigation build generation reclamation (ADR-0531)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import InvestigationState
from kdive.mcp.tools.lifecycle.runs import steps as run_steps_module
from kdive.reconciler.cleanup import gc as gc_module
from kdive.reconciler.cleanup.gc import gc_expired_build_artifacts, gc_investigation_artifacts
from kdive.services.runs.build_catalog import publish_or_reuse_build
from kdive.services.runs.steps import BuildStepResult
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
from tests.services.runs.test_build_catalog import (
    _heads as catalog_heads,
)
from tests.services.runs.test_build_catalog import (
    _result as catalog_result,
)
from tests.services.runs.test_build_catalog import (
    _run as catalog_run,
)


class _Store:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        return True

    def delete_version(self, key: str, version_id: str) -> None:
        self.deleted.append((key, version_id))


def test_run_create_pin_wins_before_reclaim_lock(migrated_url: str) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        investigation_id = uuid4()
        generation = uuid4()
        digest = "d" * 64
        build_ref = f"{digest}.{generation}"
        key = f"builds/{generation}/kernel"
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
                    build_ref,
                    digest,
                    Jsonb({"kernel": {"key": key, "version_id": "v1"}}),
                ),
            )
        finally:
            await seed.close()

        creator = await connect(migrated_url)
        reclaimer = await connect(migrated_url)
        store = _Store()
        try:
            async with (
                creator.transaction(),
                advisory_xact_lock(creator, LockScope.INVESTIGATION, investigation_id),
            ):
                task = asyncio.create_task(
                    gc_expired_build_artifacts(reclaimer, store, timedelta(days=30))
                )
                await asyncio.sleep(0)
                run_id = uuid4()
                await creator.execute(
                    "INSERT INTO runs (id, investigation_id, state, build_profile, target_kind, "
                    "principal, project, build_ref) VALUES (%s, %s, 'created', '{}'::jsonb, "
                    "'local-libvirt', 'p', 'proj', %s)",
                    (run_id, investigation_id, build_ref),
                )
            assert await task == 0
            state = await (
                await reclaimer.execute(
                    "SELECT state FROM investigation_builds WHERE generation = %s", (generation,)
                )
            ).fetchone()
            assert state == ("active",)
            assert store.deleted == []
        finally:
            await creator.close()
            await reclaimer.close()

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


def test_real_publication_wins_and_reclaim_isolates_new_same_content_generation(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        investigation_id = uuid4()
        publisher = await connect(migrated_url)
        reclaimer = await connect(migrated_url)
        try:
            await publisher.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            run = catalog_run(investigation_id)
            async with (
                publisher.transaction(),
                advisory_xact_lock(publisher, LockScope.INVESTIGATION, investigation_id),
            ):
                old = await publish_or_reuse_build(
                    publisher,
                    run=run,
                    result=catalog_result(),
                    heads=catalog_heads(),
                    retention=timedelta(days=7),
                )
            await publisher.execute(
                "UPDATE investigation_builds SET expires_at = now() - interval '1 second' "
                "WHERE generation = %s",
                (old.build.generation,),
            )
            new_result = BuildStepResult(
                kernel_ref="runs/new/kernel",
                debuginfo_ref="runs/new/vmlinux",
                initrd_ref="runs/new/initrd",
                build_id="build-id",
                cmdline="console=ttyS0",
                build_provenance={"ref": "v6.10", "dirty": False},
            )
            new_heads = {
                key.replace("runs/source/", "runs/new/"): value
                for key, value in catalog_heads().items()
            }
            store = _Store()
            async with (
                publisher.transaction(),
                advisory_xact_lock(publisher, LockScope.INVESTIGATION, investigation_id),
            ):
                fresh = await publish_or_reuse_build(
                    publisher,
                    run=run,
                    result=new_result,
                    heads=new_heads,
                    retention=timedelta(days=7),
                )
                reclaim = asyncio.create_task(
                    gc_expired_build_artifacts(reclaimer, store, timedelta(days=30))
                )
                await asyncio.sleep(0)
                assert not reclaim.done()
            assert await reclaim == 3
            rows = await (
                await publisher.execute(
                    "SELECT generation, state FROM investigation_builds "
                    "WHERE investigation_id = %s",
                    (investigation_id,),
                )
            ).fetchall()
            assert rows == [(fresh.build.generation, "active")]
            assert fresh.build.content_digest == old.build.content_digest
            assert all(not key.startswith("runs/new/") for key, _version in store.deleted)
        finally:
            await publisher.close()
            await reclaimer.close()

    asyncio.run(_run())


def test_reclaim_wins_and_real_publication_creates_fresh_generation(
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
        investigation_id = uuid4()
        publisher = await connect(migrated_url)
        reclaimer = await connect(migrated_url)
        try:
            await publisher.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            run = catalog_run(investigation_id)
            async with (
                publisher.transaction(),
                advisory_xact_lock(publisher, LockScope.INVESTIGATION, investigation_id),
            ):
                old = await publish_or_reuse_build(
                    publisher,
                    run=run,
                    result=catalog_result(),
                    heads=catalog_heads(),
                    retention=timedelta(days=7),
                )
            await publisher.execute(
                "UPDATE investigation_builds SET expires_at = now() - interval '1 second' "
                "WHERE generation = %s",
                (old.build.generation,),
            )
            reclaim = asyncio.create_task(
                gc_expired_build_artifacts(reclaimer, _Store(), timedelta(days=30))
            )
            await acquired.wait()

            async def publish_fresh():
                async with (
                    publisher.transaction(),
                    advisory_xact_lock(publisher, LockScope.INVESTIGATION, investigation_id),
                ):
                    return await publish_or_reuse_build(
                        publisher,
                        run=run,
                        result=catalog_result(),
                        heads=catalog_heads(),
                        retention=timedelta(days=7),
                    )

            publication = asyncio.create_task(publish_fresh())
            await asyncio.sleep(0)
            assert not publication.done()
            release.set()
            assert await reclaim == 3
            fresh = await publication
            assert fresh.created
            assert fresh.build.generation != old.build.generation
            assert fresh.build.content_digest == old.build.content_digest
        finally:
            await publisher.close()
            await reclaimer.close()

    with monkeypatch.context() as patched:
        patched.setattr(gc_module, "advisory_xact_lock", paused_lock)
        asyncio.run(_run())
