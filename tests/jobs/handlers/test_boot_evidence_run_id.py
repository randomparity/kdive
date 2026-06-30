"""The per-Run boot-evidence console artifact is stamped with its Run id (ADR-0279, #935)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import StoredArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.jobs.handlers.runs import boot_evidence


async def _seed_run(conn: AsyncConnection, system_id: UUID, run_id: UUID) -> None:
    """Insert the FK chain a Run needs so a run_id-stamped artifact satisfies the FK."""
    resource_id, allocation_id, investigation_id = uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'granted', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, 'ready', '{}'::jsonb, 'p', 'proj')",
        (system_id, allocation_id),
    )
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (investigation_id,),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) "
        "VALUES (%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )


def _stored(system_id: UUID, run_id: UUID, etag: str) -> StoredArtifact:
    key = f"local/systems/{system_id}/console-{run_id}"
    return StoredArtifact(key, etag, Sensitivity.REDACTED, "console")


def test_boot_evidence_row_carries_run_id(migrated_url: str) -> None:
    """The console-<run_id> row persists run_id = that Run, exactly."""
    system_id, run_id = uuid4(), uuid4()

    async def _run() -> UUID | None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                await _seed_run(conn, system_id, run_id)
                artifact = await boot_evidence._upsert_console_artifact_row(
                    conn, system_id, run_id, _stored(system_id, run_id, "etag-1"), b"boot bytes"
                )
                row = await (
                    await conn.execute("SELECT run_id FROM artifacts WHERE id = %s", (artifact.id,))
                ).fetchone()
        return None if row is None else row[0]

    assert asyncio.run(_run()) == run_id


def test_boot_evidence_recapture_keeps_run_id(migrated_url: str) -> None:
    """A same-Run re-capture (changed etag) refreshes the row and keeps its run_id."""
    system_id, run_id = uuid4(), uuid4()

    async def _run() -> UUID | None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                await _seed_run(conn, system_id, run_id)
                first = await boot_evidence._upsert_console_artifact_row(
                    conn, system_id, run_id, _stored(system_id, run_id, "etag-1"), b"boot bytes"
                )
                second = await boot_evidence._upsert_console_artifact_row(
                    conn, system_id, run_id, _stored(system_id, run_id, "etag-2"), b"boot bytes"
                )
                assert second.id == first.id  # same Run re-capture refreshes its own row
                row = await (
                    await conn.execute("SELECT run_id FROM artifacts WHERE id = %s", (second.id,))
                ).fetchone()
        return None if row is None else row[0]

    assert asyncio.run(_run()) == run_id
