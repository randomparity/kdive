"""DB-backed tests for the v1 report sections (ADR-0208).

Sections are driven directly against a migrated disposable Postgres, seeded through the
repository helpers (and raw SQL for the shape label / shape catalog, which the System
model does not carry). The async bodies run via ``asyncio.run`` per the repo convention.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import (
    ALLOCATIONS,
    BUDGETS,
    IMAGE_CATALOG,
    INVESTIGATIONS,
    RESOURCES,
    RUNS,
    SYSTEMS,
)
from kdive.domain.accounting.records import Budget
from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, Investigation, Run, System
from kdive.services.accounting import ledger as accounting
from kdive.services.reports.core import ReportScope
from kdive.services.reports.sections import (
    ActivitySection,
    CostsSection,
    ImagesSection,
    InventorySection,
    LeasesSection,
)

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
_SHAPE = "rpt-small"


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _scope() -> ReportScope:
    return ReportScope(projects=("proj",), all_projects=False)


async def _resource(conn: AsyncConnection) -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_AS_OF,
            updated_at=_AS_OF,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _allocation(
    conn: AsyncConnection,
    resource_id: UUID,
    *,
    project: str,
    state: AllocationState,
    lease_expiry: datetime | None,
) -> Allocation:
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_AS_OF,
            updated_at=_AS_OF,
            principal="user-1",
            project=project,
            resource_id=resource_id,
            state=state,
            lease_expiry=lease_expiry,
            shape=_SHAPE,
        ),
    )


async def _system(
    conn: AsyncConnection, allocation_id: UUID, *, project: str, shape: str | None, name: str
) -> System:
    system = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=_AS_OF,
            updated_at=_AS_OF,
            principal="user-1",
            project=project,
            allocation_id=allocation_id,
            state=SystemState.READY,
            provisioning_profile={},
        ),
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE systems SET shape = %s, domain_name = %s WHERE id = %s",
            (shape, name, system.id),
        )
    return system


async def _seed_shape(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
            (_SHAPE, 2, 4096, 40),
        )


def test_inventory_known_and_unknown_shape(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await _seed_shape(conn)
            res = await _resource(conn)
            alloc = await _allocation(
                conn, res.id, project="proj", state=AllocationState.ACTIVE, lease_expiry=None
            )
            await _system(conn, alloc.id, project="proj", shape=_SHAPE, name="vm-known")
            await _system(conn, alloc.id, project="proj", shape="ghost", name="vm-unknown")
            rows = (await InventorySection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
        by_name = {row["name"]: row for row in rows}
        known = by_name["vm-known"]
        assert (known["vcpus"], known["memory_mb"], known["disk_gb"]) == (2, 4096, 40)
        assert known["resource_kind"] == ResourceKind.LOCAL_LIBVIRT.value
        unknown = by_name["vm-unknown"]
        assert unknown["vcpus"] is None
        assert unknown["memory_mb"] is None
        assert unknown["disk_gb"] is None

    asyncio.run(_run())


def test_leases_active_stale_boundary_against_as_of(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            res = await _resource(conn)
            at_boundary = await _allocation(
                conn, res.id, project="proj", state=AllocationState.ACTIVE, lease_expiry=_AS_OF
            )
            just_after = await _allocation(
                conn,
                res.id,
                project="proj",
                state=AllocationState.ACTIVE,
                lease_expiry=_AS_OF + timedelta(seconds=1),
            )
            expired = await _allocation(
                conn,
                res.id,
                project="proj",
                state=AllocationState.EXPIRED,
                lease_expiry=_AS_OF - timedelta(hours=1),
            )
            rows = (await LeasesSection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
        status = {row["allocation_id"]: row["status"] for row in rows}
        assert status[at_boundary.id] == "stale"  # lease_expiry == as_of is not > as_of
        assert status[just_after.id] == "active"
        assert status[expired.id] == "stale"

    asyncio.run(_run())


def test_images_visibility_scope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await _image(conn, name="pub", visibility=ImageVisibility.PUBLIC, owner=None)
            await _image(conn, name="mine", visibility=ImageVisibility.PRIVATE, owner="proj")
            await _image(conn, name="theirs", visibility=ImageVisibility.PRIVATE, owner="other")
            rows = (await ImagesSection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
        names = {row["name"] for row in rows}
        assert "pub" in names
        assert "mine" in names
        assert "theirs" not in names

    asyncio.run(_run())


def test_activity_half_open_window_and_cap(migrated_url: str) -> None:
    async def _run() -> None:
        start = _AS_OF - timedelta(hours=2)
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            inv = await _investigation(conn)
            res = await _resource(conn)
            alloc = await _allocation(
                conn, res.id, project="proj", state=AllocationState.ACTIVE, lease_expiry=None
            )
            system = await _system(conn, alloc.id, project="proj", shape=None, name="vm-activity")
            await _run_row(conn, inv.id, system.id, created_at=start)  # included (>= start)
            await _run_row(conn, inv.id, system.id, created_at=_AS_OF - timedelta(hours=1))
            await _run_row(conn, inv.id, system.id, created_at=_AS_OF)  # excluded (== end)
            windowed = await ActivitySection().gather(
                conn, _scope(), (start, _AS_OF), _AS_OF, cap=500
            )
            capped = await ActivitySection().gather(conn, _scope(), (start, _AS_OF), _AS_OF, cap=1)
        times = [row["created_at"] for row in windowed.rows]
        assert times
        assert all(isinstance(t, datetime) and start <= t < _AS_OF for t in times)
        assert len(windowed.rows) == 2
        assert capped.truncated is True
        assert len(capped.rows) == 1

    asyncio.run(_run())


def test_costs_reuses_ledger_rollup(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await BUDGETS.upsert(
                conn,
                Budget(
                    project="proj",
                    limit_kcu=Decimal("100"),
                    spent_kcu=Decimal(0),
                    updated_at=_AS_OF,
                ),
            )
            res = await _resource(conn)
            alloc = await _allocation(
                conn, res.id, project="proj", state=AllocationState.ACTIVE, lease_expiry=None
            )
            await accounting.reserve(conn, alloc, Decimal("9.0000"))
            await accounting.reconcile(conn, alloc)
            rows = (await CostsSection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
        assert rows
        row = rows[0]
        assert {"project", "principal", "reserved", "reconciled", "variance"} <= set(row)
        assert row["project"] == "proj"

    asyncio.run(_run())


async def _image(
    conn: AsyncConnection, *, name: str, visibility: ImageVisibility, owner: str | None
) -> None:
    expires_at = None if owner is None else _AS_OF + timedelta(days=30)
    await IMAGE_CATALOG.insert(
        conn,
        ImageCatalogEntry(
            id=uuid4(),
            created_at=_AS_OF,
            updated_at=_AS_OF,
            provider="local-libvirt",
            name=name,
            arch="x86_64",
            format="qcow2",
            root_device="/dev/vda1",
            visibility=visibility,
            owner=owner,
            expires_at=expires_at,
            state=ImageState.DEFINED,
            pending_since=_AS_OF,
        ),
    )


async def _investigation(conn: AsyncConnection) -> Investigation:
    return await INVESTIGATIONS.insert(
        conn,
        Investigation(
            id=uuid4(),
            created_at=_AS_OF,
            updated_at=_AS_OF,
            principal="user-1",
            project="proj",
            title="inv",
            state=InvestigationState.OPEN,
        ),
    )


async def _run_row(
    conn: AsyncConnection, investigation_id: UUID, system_id: UUID, *, created_at: datetime
) -> None:
    run = await RUNS.insert(
        conn,
        Run(
            id=uuid4(),
            created_at=_AS_OF,
            updated_at=_AS_OF,
            principal="user-1",
            project="proj",
            investigation_id=investigation_id,
            system_id=system_id,
            target_kind=ResourceKind.LOCAL_LIBVIRT,
            state=RunState.CREATED,
            build_profile={},
        ),
    )
    async with conn.cursor() as cur:
        await cur.execute("UPDATE runs SET created_at = %s WHERE id = %s", (created_at, run.id))
