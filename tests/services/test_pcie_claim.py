"""Active PCIe occupancy from non-terminal allocations (ADR-0068), using real Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg

import kdive.services.allocation.admission.pcie_claim as pcie_claim
from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.capacity.state import AllocationState, ResourceStatus
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation
from kdive.domain.pcie import PCIE_DEVICES_KEY, PCIeClaim, PCIeDescriptor

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_X710 = PCIeDescriptor(
    bdf="0000:3b:00.0", vendor_id="8086", device_id="1572", class_code="020000", label="X710"
)
_X710_B = PCIeDescriptor(
    bdf="0000:3b:00.1", vendor_id="8086", device_id="1572", class_code="020000", label="X710"
)


@asynccontextmanager
async def _conn(url: str) -> AsyncGenerator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


def _resource(devices: object) -> Resource:
    return Resource(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=ResourceKind.LOCAL_LIBVIRT,
        capabilities={PCIE_DEVICES_KEY: devices},
        pool="local-libvirt",
        cost_class="local",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
    )


async def _seed_alloc(
    conn: psycopg.AsyncConnection,
    resource_id: UUID,
    state: AllocationState,
    claim: list[PCIeClaim],
) -> Allocation:
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource_id,
            state=state,
            pcie_claim=claim,
        ),
    )


async def _seed_host(conn: psycopg.AsyncConnection) -> Resource:
    return await RESOURCES.insert(conn, _resource([dict(_X710), dict(_X710_B)]))


def test_active_claims_unions_non_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            host = await _seed_host(conn)
            await _seed_alloc(
                conn,
                host.id,
                AllocationState.GRANTED,
                [PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
            )
            claims = await pcie_claim.active_claims(conn, host.id)
            # Assert the full claim identity, not just the bdf: the matcher subtracts by bdf,
            # but vendor_id/device_id must round-trip intact from the persisted snapshot.
            assert claims == [PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")]

    asyncio.run(_run())


def test_active_claims_excludes_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            host = await _seed_host(conn)
            for state in (
                AllocationState.RELEASED,
                AllocationState.EXPIRED,
                AllocationState.FAILED,
            ):
                await _seed_alloc(
                    conn,
                    host.id,
                    state,
                    [PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
                )
            assert await pcie_claim.active_claims(conn, host.id) == []

    asyncio.run(_run())


def test_active_claims_counts_releasing(migrated_url: str) -> None:
    # RELEASING is non-terminal: its claim still occupies the device (challenge finding 2).
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            host = await _seed_host(conn)
            await _seed_alloc(
                conn,
                host.id,
                AllocationState.RELEASING,
                [PCIeClaim(bdf="0000:3b:00.1", vendor_id="8086", device_id="1572")],
            )
            claims = await pcie_claim.active_claims(conn, host.id)
            assert [c["bdf"] for c in claims] == ["0000:3b:00.1"]

    asyncio.run(_run())


def test_active_claims_is_host_scoped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            host = await _seed_host(conn)
            other = await _seed_host(conn)
            await _seed_alloc(
                conn,
                other.id,
                AllocationState.GRANTED,
                [PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
            )
            assert await pcie_claim.active_claims(conn, host.id) == []

    asyncio.run(_run())
