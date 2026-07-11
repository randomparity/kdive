"""Inventory prune/cordon helper branch contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.domain.capacity.state import AllocationState, SystemState
from kdive.domain.catalog.resources import ManagedBy, ResourceKind
from kdive.inventory.reconcile.prune import (
    prune_or_cordon_image,
    prune_or_cordon_removed_resource,
    prune_or_cordon_resource,
)

_KIND = ResourceKind.LOCAL_LIBVIRT
_LEASE = datetime(2026, 1, 1, tzinfo=UTC)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _insert_image(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    managed_by: ManagedBy = ManagedBy.CONFIG,
) -> UUID:
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, visibility, state, object_key, managed_by) "
        "VALUES ('local-libvirt', %s, 'x86_64', 'qcow2', '/dev/vda', 'public', "
        "'registered', %s, %s) RETURNING id",
        (name, f"images/{name}.qcow2", managed_by.value),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_resource(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    managed_by: ManagedBy = ManagedBy.CONFIG,
    cordoned: bool = False,
    lease_expires_at: datetime | None = None,
) -> UUID:
    cur = await conn.execute(
        "INSERT INTO resources "
        "(kind, name, pool, cost_class, status, host_uri, managed_by, cordoned, lease_expires_at) "
        "VALUES (%s, %s, 'local-libvirt', 'local', 'available', 'qemu:///system', "
        "%s, %s, %s) RETURNING id",
        (_KIND.value, name, managed_by.value, cordoned, lease_expires_at),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_allocation(
    conn: psycopg.AsyncConnection, resource_id: UUID, state: AllocationState
) -> UUID:
    cur = await conn.execute(
        "INSERT INTO allocations (resource_id, state, principal, project) "
        "VALUES (%s, %s, 'alice', 'proj') RETURNING id",
        (resource_id, state.value),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_non_terminal_system(
    conn: psycopg.AsyncConnection, *, provider: str, image_name: str
) -> None:
    resource_id = await _insert_resource(conn, name=f"system-host-{uuid4()}")
    allocation_id = await _insert_allocation(conn, resource_id, AllocationState.ACTIVE)
    profile = {
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "catalog", "provider": provider, "name": image_name}
            }
        }
    }
    await conn.execute(
        "INSERT INTO systems (principal, project, allocation_id, state, provisioning_profile) "
        "VALUES ('alice', 'proj', %s, %s, %s)",
        (allocation_id, SystemState.READY.value, Jsonb(profile)),
    )


async def _image_exists(conn: psycopg.AsyncConnection, row_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
    return await cur.fetchone() is not None


async def _resource_row(conn: psycopg.AsyncConnection, row_id: UUID) -> dict[str, object] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT cordoned, lease_expires_at FROM resources WHERE id = %s",
            (row_id,),
        )
        return await cur.fetchone()


def test_prune_image_absent_and_non_config_are_noops(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            missing = await prune_or_cordon_image(conn, uuid4())
            discovery = await _insert_image(
                conn, name=f"discovery-{uuid4()}", managed_by=ManagedBy.DISCOVERY
            )
            outcome = await prune_or_cordon_image(conn, discovery)

            assert missing.pruned is False
            assert missing.cordoned is False
            assert outcome.pruned is False
            assert outcome.cordoned is False
            assert await _image_exists(conn, discovery) is True

    asyncio.run(_run())


def test_prune_image_deletes_idle_config_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            image_id = await _insert_image(conn, name=f"idle-{uuid4()}")
            outcome = await prune_or_cordon_image(conn, image_id)

            assert outcome.pruned is True
            assert outcome.cordoned is False
            assert await _image_exists(conn, image_id) is False

    asyncio.run(_run())


def test_prune_image_cordons_when_live_system_references_it(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            name = f"referenced-{uuid4()}"
            image_id = await _insert_image(conn, name=name)
            await _insert_non_terminal_system(conn, provider="local-libvirt", image_name=name)
            outcome = await prune_or_cordon_image(conn, image_id)

            assert outcome.pruned is False
            assert outcome.cordoned is True
            assert await _image_exists(conn, image_id) is True

    asyncio.run(_run())


def test_prune_resource_absent_and_non_config_are_noops(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            missing = await prune_or_cordon_resource(conn, uuid4(), "missing", kind=_KIND)
            discovery = await _insert_resource(
                conn, name=f"discovery-{uuid4()}", managed_by=ManagedBy.DISCOVERY
            )
            outcome = await prune_or_cordon_resource(conn, discovery, "unused-name", kind=_KIND)

            assert missing.pruned is False
            assert missing.cordoned is False
            assert outcome.pruned is False
            assert outcome.cordoned is False
            assert await _resource_row(conn, discovery) is not None

    asyncio.run(_run())


def test_prune_resource_deletes_idle_config_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            name = f"idle-{uuid4()}"
            resource_id = await _insert_resource(conn, name=name)
            outcome = await prune_or_cordon_resource(conn, resource_id, name, kind=_KIND)

            assert outcome.pruned is True
            assert outcome.cordoned is False
            assert await _resource_row(conn, resource_id) is None

    asyncio.run(_run())


def test_prune_resource_cordons_live_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            name = f"live-{uuid4()}"
            resource_id = await _insert_resource(conn, name=name)
            await _insert_allocation(conn, resource_id, AllocationState.ACTIVE)
            outcome = await prune_or_cordon_resource(conn, resource_id, name, kind=_KIND)
            row = await _resource_row(conn, resource_id)

            assert outcome.pruned is False
            assert outcome.cordoned is True
            assert row is not None
            assert row["cordoned"] is True

    asyncio.run(_run())


def test_removed_resource_with_allocation_history_cordons_and_clears_lease(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            name = f"historic-{uuid4()}"
            resource_id = await _insert_resource(conn, name=name, lease_expires_at=_LEASE)
            await _insert_allocation(conn, resource_id, AllocationState.RELEASED)
            outcome = await prune_or_cordon_removed_resource(conn, resource_id, name, kind=_KIND)
            row = await _resource_row(conn, resource_id)

            assert outcome.pruned is False
            assert outcome.cordoned is True
            assert row is not None
            assert row["cordoned"] is True
            assert row["lease_expires_at"] is None

    asyncio.run(_run())


def test_removed_resource_without_allocations_deletes(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            name = f"removed-idle-{uuid4()}"
            resource_id = await _insert_resource(conn, name=name)
            outcome = await prune_or_cordon_removed_resource(conn, resource_id, name, kind=_KIND)

            assert outcome.pruned is True
            assert outcome.cordoned is False
            assert await _resource_row(conn, resource_id) is None

    asyncio.run(_run())


def test_removed_resource_already_cordoned_reports_no_new_cordon(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            name = f"already-cordoned-{uuid4()}"
            resource_id = await _insert_resource(conn, name=name, cordoned=True)
            await _insert_allocation(conn, resource_id, AllocationState.RELEASED)
            outcome = await prune_or_cordon_removed_resource(conn, resource_id, name, kind=_KIND)
            row = await _resource_row(conn, resource_id)

            assert outcome.pruned is False
            assert outcome.cordoned is False
            assert row is not None
            assert row["cordoned"] is True

    asyncio.run(_run())
