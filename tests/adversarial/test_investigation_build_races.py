"""Concurrency barriers for Investigation build generation reclamation (ADR-0531)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.reconciler.cleanup.gc import gc_expired_build_artifacts
from tests.reconciler.conftest import connect


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
