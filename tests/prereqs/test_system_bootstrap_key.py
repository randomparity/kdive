"""Unit tests for the per-System SSH bootstrap keypair service (ADR-0289, #963)."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.prereqs.system_bootstrap_key import (
    delete_system_bootstrap_key,
    ensure_system_bootstrap_key,
    generate_keypair,
    load_system_bootstrap_private_key,
    materialized_private_key,
)


def test_generate_keypair_returns_ed25519_pair_and_leaves_no_scratch() -> None:
    private_pem, public_openssh = generate_keypair()
    assert "OPENSSH PRIVATE KEY" in private_pem
    assert public_openssh.startswith("ssh-ed25519 ")


def test_materialized_private_key_is_0600_and_cleaned_up() -> None:
    seen: Path | None = None
    with materialized_private_key("KEY-MATERIAL\n") as key_path:
        seen = key_path
        assert key_path.read_text() == "KEY-MATERIAL\n"
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert seen is not None and not seen.exists()


def test_materialized_private_key_cleans_up_on_exception() -> None:
    captured: Path | None = None
    with pytest.raises(RuntimeError), materialized_private_key("K\n") as key_path:
        captured = key_path
        raise RuntimeError("boom")
    assert captured is not None and not captured.exists()


# Async-DB tests follow the PROVEN in-repo pattern in
# tests/jobs/handlers/test_boot_evidence_run_id.py: a SYNC `def test_(migrated_url)` with an
# inner `async def _run()` driven by `asyncio.run(_run())`, a conn from
# `AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False)`, and an
# `async def _seed_system(conn: AsyncConnection)` using the real FK chain. There is NO
# `asyncio_mode=auto` in this repo — do NOT write bare `async def test_*` or `async def` fixtures.


async def _seed_system(conn: AsyncConnection) -> UUID:
    """Seed the resources -> allocations -> systems FK chain; return the system_id.

    Mirrors _seed_run in tests/jobs/handlers/test_boot_evidence_run_id.py (allocations requires a
    NOT NULL resource_id -> resources(id); systems requires allocation_id + provisioning_profile).
    """
    resource_id, allocation_id, system_id = uuid4(), uuid4(), uuid4()
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
    return system_id


def test_ensure_is_idempotent_one_row_one_pubkey(migrated_url: str) -> None:
    async def _run() -> tuple[str, str, int]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                first = await ensure_system_bootstrap_key(conn, system_id)
                second = await ensure_system_bootstrap_key(conn, system_id)
                row = await (
                    await conn.execute(
                        "SELECT count(*) FROM system_bootstrap_keys WHERE system_id = %s",
                        (system_id,),
                    )
                ).fetchone()
                assert row is not None
                return first, second, row[0]

    first, second, count = asyncio.run(_run())
    assert first == second and first.startswith("ssh-ed25519 ") and count == 1


def test_load_returns_private_key_and_raises_when_absent(migrated_url: str) -> None:
    async def _run() -> str:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                with pytest.raises(CategorizedError) as excinfo:
                    await load_system_bootstrap_private_key(conn, system_id)
                assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
                await ensure_system_bootstrap_key(conn, system_id)
                return await load_system_bootstrap_private_key(conn, system_id)

    assert "OPENSSH PRIVATE KEY" in asyncio.run(_run())


def test_delete_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                await ensure_system_bootstrap_key(conn, system_id)
                await delete_system_bootstrap_key(conn, system_id)
                await delete_system_bootstrap_key(conn, system_id)  # no-op, no raise
                with pytest.raises(CategorizedError):
                    await load_system_bootstrap_private_key(conn, system_id)

    asyncio.run(_run())
