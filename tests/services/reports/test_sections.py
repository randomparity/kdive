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
from kdive.services.reports.core import ReportScope, SectionRows, Window
from kdive.services.reports.sections import (
    ActivitySection,
    CostsSection,
    ImagesSection,
    InventorySection,
    LeasesSection,
    _capped,
    _effective_window,
    _window_clause,
)

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
_SHAPE = "rpt-small"


def test_capped_flags_truncation_only_above_cap() -> None:
    rows: list[dict[str, object]] = [{"a": 1}, {"a": 2}, {"a": 3}]
    exact = _capped(rows[:2], 2)
    assert exact == SectionRows(rows=({"a": 1}, {"a": 2}), truncated=False)
    over = _capped(rows, 2)
    assert over.rows == ({"a": 1}, {"a": 2})
    assert over.truncated is True


def test_window_clause_appends_each_present_bound() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 22, tzinfo=UTC)

    empty: list[object] = []
    assert _window_clause(None, "created_at", empty) == ""
    assert empty == []

    start_only: list[object] = []
    assert _window_clause((start, None), "created_at", start_only) == " AND created_at >= %s"
    assert start_only == [start]

    end_only: list[object] = []
    assert _window_clause((None, end), "created_at", end_only) == " AND created_at < %s"
    assert end_only == [end]

    both: list[object] = []
    assert (
        _window_clause((start, end), "created_at", both)
        == " AND created_at >= %s AND created_at < %s"
    )
    assert both == [start, end]


def test_effective_window_defaults_open_end_to_as_of() -> None:
    start = _AS_OF - timedelta(hours=1)
    explicit_end = _AS_OF + timedelta(hours=5)
    # No window at all becomes a point-in-time upper bound at as_of.
    assert _effective_window(None, _AS_OF) == (None, _AS_OF)
    # An open upper bound defaults to as_of; the start bound is preserved.
    assert _effective_window((start, None), _AS_OF) == (start, _AS_OF)
    # A fully bounded window is returned unchanged (as_of is not substituted).
    assert _effective_window((start, explicit_end), _AS_OF) == (start, explicit_end)


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


def _scope_all() -> ReportScope:
    return ReportScope(projects=(), all_projects=True)


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


async def _custom_allocation(
    conn: AsyncConnection,
    resource_id: UUID,
    *,
    project: str,
    vcpus: int,
    memory_gb: int,
    disk_gb: int,
) -> Allocation:
    """A full-custom allocation: stamped requested_* sizing, no shape label (ADR-0067)."""
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_AS_OF,
            updated_at=_AS_OF,
            principal="user-1",
            project=project,
            resource_id=resource_id,
            state=AllocationState.ACTIVE,
            lease_expiry=None,
            shape=None,
            requested_vcpus=vcpus,
            requested_memory_gb=memory_gb,
            requested_disk_gb=disk_gb,
        ),
    )


def test_inventory_reports_stamped_size_for_custom_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            res = await _resource(conn)
            alloc = await _custom_allocation(
                conn, res.id, project="proj", vcpus=8, memory_gb=16, disk_gb=100
            )
            await _system(conn, alloc.id, project="proj", shape=None, name="vm-custom")
            rows = (await InventorySection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
        row = next(r for r in rows if r["name"] == "vm-custom")
        # A custom-sized System (no shape) reports its stamped size, not NULL: memory in MB.
        assert (row["vcpus"], row["memory_mb"], row["disk_gb"]) == (8, 16384, 100)

    asyncio.run(_run())


def test_inventory_legacy_null_stamp_no_shape_reports_null(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            res = await _resource(conn)
            # A legacy allocation predating the requested_* snapshot columns: no stamp, no shape.
            alloc = await _custom_allocation(
                conn, res.id, project="proj", vcpus=2, memory_gb=4, disk_gb=20
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE allocations SET requested_vcpus = NULL, requested_memory_gb = NULL, "
                    "requested_disk_gb = NULL WHERE id = %s",
                    (alloc.id,),
                )
            await _system(conn, alloc.id, project="proj", shape=None, name="vm-legacy")
            rows = (await InventorySection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
        row = next(r for r in rows if r["name"] == "vm-legacy")
        assert (row["vcpus"], row["memory_mb"], row["disk_gb"]) == (None, None, None)

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


def test_inventory_scopes_out_other_projects(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await _seed_shape(conn)
            res = await _resource(conn)
            mine = await _allocation(
                conn, res.id, project="proj", state=AllocationState.ACTIVE, lease_expiry=None
            )
            other = await _allocation(
                conn, res.id, project="other", state=AllocationState.ACTIVE, lease_expiry=None
            )
            await _system(conn, mine.id, project="proj", shape=_SHAPE, name="vm-mine")
            await _system(conn, other.id, project="other", shape=_SHAPE, name="vm-other")
            scoped = (await InventorySection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
            allp = (await InventorySection().gather(conn, _scope_all(), None, _AS_OF, cap=500)).rows
        scoped_names = {row["name"] for row in scoped}
        assert "vm-mine" in scoped_names
        assert "vm-other" not in scoped_names
        all_names = {row["name"] for row in allp}
        assert {"vm-mine", "vm-other"} <= all_names

    asyncio.run(_run())


def test_inventory_truncates_at_cap(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await _seed_shape(conn)
            res = await _resource(conn)
            alloc = await _allocation(
                conn, res.id, project="proj", state=AllocationState.ACTIVE, lease_expiry=None
            )
            for i in range(3):
                await _system(conn, alloc.id, project="proj", shape=_SHAPE, name=f"vm-{i}")
            capped = await InventorySection().gather(conn, _scope(), None, _AS_OF, cap=1)
        assert capped.truncated is True
        assert len(capped.rows) == 1

    asyncio.run(_run())


def test_leases_scopes_out_other_projects(migrated_url: str) -> None:
    async def _run() -> None:
        expiry = _AS_OF + timedelta(hours=1)
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            res = await _resource(conn)
            mine = await _allocation(
                conn, res.id, project="proj", state=AllocationState.ACTIVE, lease_expiry=expiry
            )
            other = await _allocation(
                conn, res.id, project="other", state=AllocationState.ACTIVE, lease_expiry=expiry
            )
            scoped = (await LeasesSection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
            allp = (await LeasesSection().gather(conn, _scope_all(), None, _AS_OF, cap=500)).rows
        scoped_ids = {row["allocation_id"] for row in scoped}
        assert mine.id in scoped_ids
        assert other.id not in scoped_ids
        all_ids = {row["allocation_id"] for row in allp}
        assert {mine.id, other.id} <= all_ids

    asyncio.run(_run())


def test_leases_truncates_at_cap(migrated_url: str) -> None:
    async def _run() -> None:
        expiry = _AS_OF + timedelta(hours=1)
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            res = await _resource(conn)
            for _ in range(3):
                await _allocation(
                    conn,
                    res.id,
                    project="proj",
                    state=AllocationState.ACTIVE,
                    lease_expiry=expiry,
                )
            capped = await LeasesSection().gather(conn, _scope(), None, _AS_OF, cap=1)
        assert capped.truncated is True
        assert len(capped.rows) == 1

    asyncio.run(_run())


def test_images_all_projects_includes_foreign_private(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await _image(conn, name="pub", visibility=ImageVisibility.PUBLIC, owner=None)
            await _image(conn, name="mine", visibility=ImageVisibility.PRIVATE, owner="proj")
            await _image(conn, name="theirs", visibility=ImageVisibility.PRIVATE, owner="other")
            rows = (await ImagesSection().gather(conn, _scope_all(), None, _AS_OF, cap=500)).rows
        names = {row["name"] for row in rows}
        assert {"pub", "mine", "theirs"} <= names

    asyncio.run(_run())


def test_images_truncates_at_cap(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            for i in range(3):
                await _image(conn, name=f"pub-{i}", visibility=ImageVisibility.PUBLIC, owner=None)
            capped = await ImagesSection().gather(conn, _scope(), None, _AS_OF, cap=1)
        assert capped.truncated is True
        assert len(capped.rows) == 1

    asyncio.run(_run())


def test_activity_effective_window_bounds_and_scope(migrated_url: str) -> None:
    async def _run() -> None:
        start = _AS_OF - timedelta(hours=2)
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            inv = await _investigation(conn)
            res = await _resource(conn)
            alloc = await _allocation(
                conn, res.id, project="proj", state=AllocationState.ACTIVE, lease_expiry=None
            )
            system = await _system(conn, alloc.id, project="proj", shape=None, name="vm-activity")
            before = await _run_row(conn, inv.id, system.id, created_at=start - timedelta(hours=1))
            inside = await _run_row(conn, inv.id, system.id, created_at=_AS_OF - timedelta(hours=1))
            at_end = await _run_row(conn, inv.id, system.id, created_at=_AS_OF)
            other = await _run_row(
                conn,
                inv.id,
                system.id,
                created_at=_AS_OF - timedelta(hours=1),
                project="other",
            )
            # Open upper bound: end defaults to as_of, so at_end (== as_of) is excluded.
            window: Window = (start, None)
            scoped = await ActivitySection().gather(conn, _scope(), window, _AS_OF, cap=500)
            allp = await ActivitySection().gather(conn, _scope_all(), window, _AS_OF, cap=500)
        scoped_ids = {row["run_id"] for row in scoped.rows}
        assert inside.id in scoped_ids
        assert before.id not in scoped_ids
        assert at_end.id not in scoped_ids
        assert other.id not in scoped_ids
        all_ids = {row["run_id"] for row in allp.rows}
        assert inside.id in all_ids
        assert other.id in all_ids
        assert before.id not in all_ids
        assert at_end.id not in all_ids

    asyncio.run(_run())


async def _reserved_alloc(conn: AsyncConnection, amount: Decimal) -> None:
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
    await accounting.reserve(conn, alloc, amount)
    await accounting.reconcile(conn, alloc)


def test_costs_reports_real_per_principal_values(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await _reserved_alloc(conn, Decimal("9.0000"))
            rows = (await CostsSection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
        row = next(r for r in rows if r["project"] == "proj")
        # Per-principal grouping keeps the real principal, not a blanked-out placeholder.
        assert row["principal"] == "user-1"
        assert Decimal(str(row["reserved"])) == Decimal("9.0000")
        # reconciled/variance carry real Decimal strings, never str(None) == "None".
        assert row["reconciled"] != "None"
        assert row["variance"] != "None"
        # variance == reconciled - reserved (ledger rollup), so the fields stay consistent.
        assert Decimal(str(row["reconciled"])) == Decimal(str(row["reserved"])) + Decimal(
            str(row["variance"])
        )

    asyncio.run(_run())


def test_costs_window_excludes_out_of_range_ledger(migrated_url: str) -> None:
    async def _run() -> None:
        past: tuple[datetime, datetime] = (
            _AS_OF - timedelta(days=400),
            _AS_OF - timedelta(days=399),
        )
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await _reserved_alloc(conn, Decimal("9.0000"))
            all_time = (await CostsSection().gather(conn, _scope(), None, _AS_OF, cap=500)).rows
            windowed = (await CostsSection().gather(conn, _scope(), past, _AS_OF, cap=500)).rows
        assert any(row["project"] == "proj" for row in all_time)
        assert windowed == ()

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
    conn: AsyncConnection,
    investigation_id: UUID,
    system_id: UUID,
    *,
    created_at: datetime,
    project: str = "proj",
) -> Run:
    run = await RUNS.insert(
        conn,
        Run(
            id=uuid4(),
            created_at=_AS_OF,
            updated_at=_AS_OF,
            principal="user-1",
            project=project,
            investigation_id=investigation_id,
            system_id=system_id,
            target_kind=ResourceKind.LOCAL_LIBVIRT,
            state=RunState.CREATED,
            build_profile={},
        ),
    )
    async with conn.cursor() as cur:
        await cur.execute("UPDATE runs SET created_at = %s WHERE id = %s", (created_at, run.id))
    return run
