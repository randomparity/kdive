"""Run-scoped console manifest listing (ADR-0279, #935)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.services.artifacts.listing import (
    CONSOLE_MANIFEST_MAX,
    ConsoleManifest,
    list_run_console_artifacts,
)
from tests.mcp._seed import seed_crashed_system, seed_run_on_system

_DT = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _console_artifact(
    conn: AsyncConnection,
    system_id: str,
    run_id: str | None,
    name: str,
    *,
    created: datetime,
) -> None:
    await conn.execute(
        "INSERT INTO artifacts (created_at, updated_at, owner_kind, owner_id, object_key, etag, "
        "sensitivity, retention_class, run_id) "
        "VALUES (%s, %s, 'systems', %s, %s, 'e', 'redacted', 'console', %s)",
        (created, created, system_id, f"local/systems/{system_id}/{name}", run_id),
    )


def test_manifest_lists_correlated_console_newest_first(migrated_url: str) -> None:
    async def _run() -> tuple[ConsoleManifest, str]:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id=None)
            async with pool.connection() as conn:
                await _console_artifact(conn, system_id, run_id, f"console-{run_id}", created=_DT)
                await _console_artifact(
                    conn,
                    system_id,
                    run_id,
                    "console-part-0-000000",
                    created=_DT + timedelta(seconds=10),
                )
                await _console_artifact(
                    conn,
                    system_id,
                    run_id,
                    "console-part-0-000001",
                    created=_DT + timedelta(seconds=20),
                )
                # An uncorrelated part on the same System must NOT appear.
                await _console_artifact(
                    conn,
                    system_id,
                    None,
                    "console-part-9-000000",
                    created=_DT + timedelta(seconds=30),
                )
                return await list_run_console_artifacts(conn, run_id), run_id

    manifest, run_id = asyncio.run(_run())
    assert manifest.total == 3
    keys = [e["object_key"].rsplit("/", 1)[-1] for e in manifest.entries]
    # newest-first by created_at: parts (t=20, t=10) precede the boot snapshot (t=0); the
    # uncorrelated part is excluded.
    assert keys == ["console-part-0-000001", "console-part-0-000000", f"console-{run_id}"]
    for entry in manifest.entries:
        assert set(entry) == {"artifact_id", "object_key", "created_at"}


def test_manifest_total_order_same_created_at(migrated_url: str) -> None:
    """Two artifacts sharing created_at order deterministically by object_key DESC."""

    async def _run() -> list[str]:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id=None)
            async with pool.connection() as conn:
                await _console_artifact(
                    conn, system_id, run_id, "console-part-0-000000", created=_DT
                )
                await _console_artifact(
                    conn, system_id, run_id, "console-part-0-000001", created=_DT
                )
                manifest = await list_run_console_artifacts(conn, run_id)
            return [e["object_key"].rsplit("/", 1)[-1] for e in manifest.entries]

    assert asyncio.run(_run()) == ["console-part-0-000001", "console-part-0-000000"]


def test_manifest_truncates_to_cap_keeping_newest(migrated_url: str) -> None:
    async def _run() -> ConsoleManifest:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id=None)
            async with pool.connection() as conn:
                for i in range(CONSOLE_MANIFEST_MAX + 5):
                    await _console_artifact(
                        conn,
                        system_id,
                        run_id,
                        f"console-part-0-{i:06d}",
                        created=_DT + timedelta(seconds=i),
                    )
                return await list_run_console_artifacts(conn, run_id)

    manifest = asyncio.run(_run())
    assert len(manifest.entries) == CONSOLE_MANIFEST_MAX
    assert manifest.total == CONSOLE_MANIFEST_MAX + 5
    newest_key = manifest.entries[0]["object_key"].rsplit("/", 1)[-1]
    assert newest_key == f"console-part-0-{CONSOLE_MANIFEST_MAX + 4:06d}"


def test_manifest_empty_when_no_correlated_console(migrated_url: str) -> None:
    async def _run() -> ConsoleManifest:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id=None)
            async with pool.connection() as conn:
                return await list_run_console_artifacts(conn, run_id)

    manifest = asyncio.run(_run())
    assert manifest.entries == []
    assert manifest.total == 0
