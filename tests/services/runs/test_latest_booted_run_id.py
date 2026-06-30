"""latest_booted_run_id resolves a System's most-recently-booted Run (ADR-0279, #935)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.services.runs.steps import latest_booted_run_id


async def _seed_system(conn: AsyncConnection, system_id: UUID) -> UUID:
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
    return investigation_id


async def _seed_run(
    conn: AsyncConnection,
    *,
    run_id: UUID,
    investigation_id: UUID,
    system_id: UUID,
    created_at: str,
    with_boot_step: bool,
) -> None:
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project, created_at) "
        "VALUES (%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj', %s)",
        (run_id, investigation_id, system_id, created_at),
    )
    if with_boot_step:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state) VALUES (%s, 'boot', 'succeeded')",
            (run_id,),
        )


def test_returns_most_recently_created_booted_run(migrated_url: str) -> None:
    """Two booted Runs on one System: the later-created one wins."""
    system_id = uuid4()
    older, newer = uuid4(), uuid4()

    async def _run() -> UUID | None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                inv = await _seed_system(conn, system_id)
                await _seed_run(
                    conn,
                    run_id=older,
                    investigation_id=inv,
                    system_id=system_id,
                    created_at="2026-01-01T00:00:00+00",
                    with_boot_step=True,
                )
                await _seed_run(
                    conn,
                    run_id=newer,
                    investigation_id=inv,
                    system_id=system_id,
                    created_at="2026-01-02T00:00:00+00",
                    with_boot_step=True,
                )
                return await latest_booted_run_id(conn, system_id)

    assert asyncio.run(_run()) == newer


def test_unbooted_later_run_does_not_win(migrated_url: str) -> None:
    """A newer Run with no boot step does not displace the older booted Run."""
    system_id = uuid4()
    booted, unbooted = uuid4(), uuid4()

    async def _run() -> UUID | None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                inv = await _seed_system(conn, system_id)
                await _seed_run(
                    conn,
                    run_id=booted,
                    investigation_id=inv,
                    system_id=system_id,
                    created_at="2026-01-01T00:00:00+00",
                    with_boot_step=True,
                )
                await _seed_run(
                    conn,
                    run_id=unbooted,
                    investigation_id=inv,
                    system_id=system_id,
                    created_at="2026-01-02T00:00:00+00",
                    with_boot_step=False,
                )
                return await latest_booted_run_id(conn, system_id)

    assert asyncio.run(_run()) == booted


def test_no_boot_step_returns_none(migrated_url: str) -> None:
    """A System whose Run never booted resolves to None (uncorrelated)."""
    system_id = uuid4()

    async def _run() -> UUID | None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                inv = await _seed_system(conn, system_id)
                await _seed_run(
                    conn,
                    run_id=uuid4(),
                    investigation_id=inv,
                    system_id=system_id,
                    created_at="2026-01-01T00:00:00+00",
                    with_boot_step=False,
                )
                return await latest_booted_run_id(conn, system_id)

    assert asyncio.run(_run()) is None
