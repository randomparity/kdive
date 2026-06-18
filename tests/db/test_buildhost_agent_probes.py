"""DB-backed tests for the buildhost_agent_probe_guests marker repository (ADR-0167)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db import buildhost_agent_probes as probes
from kdive.db.build_hosts import BuildHostKind


async def _seed_ephemeral_host(pool: AsyncConnectionPool) -> UUID:
    host_id = uuid4()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO build_hosts (id, name, kind, workspace_root, max_concurrent, "
            "base_image_volume) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                host_id,
                f"eph-{host_id}",
                BuildHostKind.EPHEMERAL_LIBVIRT.value,
                "/build",
                1,
                "base.qcow2",
            ),
        )
    return host_id


def test_register_then_is_probe_live_true(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            host_id = await _seed_ephemeral_host(pool)
            run_id = uuid4()
            await probes.register(pool, build_host_id=host_id, run_id=run_id)
            async with pool.connection() as conn:
                assert await probes.is_probe_live(conn, run_id) is True

    asyncio.run(_run())


def test_second_live_probe_same_host_raises_inflight(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            host_id = await _seed_ephemeral_host(pool)
            await probes.register(pool, build_host_id=host_id, run_id=uuid4())
            raised = False
            try:
                await probes.register(pool, build_host_id=host_id, run_id=uuid4())
            except probes.ProbeInFlightError:
                raised = True
            assert raised is True

    asyncio.run(_run())


def test_release_frees_slot_and_is_probe_live_false(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            host_id = await _seed_ephemeral_host(pool)
            run_id = uuid4()
            probe_id = await probes.register(pool, build_host_id=host_id, run_id=run_id)
            await probes.release(pool, probe_id)
            async with pool.connection() as conn:
                assert await probes.is_probe_live(conn, run_id) is False
            # slot freed: a new probe registers without ProbeInFlightError
            await probes.register(pool, build_host_id=host_id, run_id=uuid4())

    asyncio.run(_run())


def test_stale_heartbeat_is_not_live(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            host_id = await _seed_ephemeral_host(pool)
            run_id = uuid4()
            await probes.register(pool, build_host_id=host_id, run_id=run_id)
            async with pool.connection() as conn:
                live = await probes.is_probe_live(conn, run_id, stale_after=timedelta(seconds=0))
            assert live is False

    asyncio.run(_run())


def test_heartbeat_advances_and_keeps_probe_live(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            host_id = await _seed_ephemeral_host(pool)
            run_id = uuid4()
            probe_id = await probes.register(pool, build_host_id=host_id, run_id=run_id)
            await probes.heartbeat(pool, probe_id)
            async with pool.connection() as conn:
                assert await probes.is_probe_live(conn, run_id) is True

    asyncio.run(_run())
