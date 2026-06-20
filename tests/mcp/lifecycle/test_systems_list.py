"""systems.list handler tests — the catalog view (#159).

The handler is driven directly with an injected pool. Seeding inserts Resources,
Allocations (with optional ``pcie_claim``), and Systems (with optional ``shape``) so each
filter axis and the no-leak scoping can be exercised independently.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES, SYSTEMS
from kdive.domain.accounting import Budget, Quota
from kdive.domain.capacity.state import AllocationState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle import Allocation, System
from kdive.domain.pcie import PCIeClaim
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.systems.view import (
    CUSTOM_SHAPE_SENTINEL,
    SystemsListRequest,
    list_systems,
)
from kdive.security.authz.rbac import Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"},
            "crashkernel": "256M",
        }
    },
}


def _ctx(
    *,
    projects: tuple[str, ...] = ("proj",),
    roles: dict[str, Role] | None = None,
) -> RequestContext:
    if roles is None:
        roles = {p: Role.VIEWER for p in projects}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


async def _list_systems(
    pool: AsyncConnectionPool, ctx: RequestContext, **filters: Any
) -> ToolResponse:
    return await list_systems(pool, ctx, SystemsListRequest(**filters))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_budget_quota(pool: AsyncConnectionPool, project: str) -> None:
    async with pool.connection() as conn:
        await QUOTAS.upsert(
            conn,
            Quota(
                project=project,
                max_concurrent_allocations=1_000_000,
                max_concurrent_systems=1_000_000,
                updated_at=_DT,
            ),
        )
        await BUDGETS.upsert(
            conn,
            Budget(
                project=project, limit_kcu=Decimal("1000000"), spent_kcu=Decimal(0), updated_at=_DT
            ),
        )


async def _seed_resource(pool: AsyncConnectionPool) -> UUID:
    async with pool.connection() as conn:
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="local-libvirt",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
    return res.id


async def _seed_allocation(
    pool: AsyncConnectionPool,
    *,
    project: str = "proj",
    resource_id: UUID,
    pcie_claim: list[PCIeClaim] | None = None,
) -> UUID:
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                resource_id=resource_id,
                state=AllocationState.GRANTED,
                pcie_claim=pcie_claim or [],
            ),
        )
    return alloc.id


async def _seed_system(
    pool: AsyncConnectionPool,
    *,
    allocation_id: UUID,
    project: str = "proj",
    state: SystemState = SystemState.READY,
    shape: str | None = None,
    created_at: datetime = _DT,
) -> UUID:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=created_at,
                updated_at=created_at,
                principal="user-1",
                project=project,
                allocation_id=allocation_id,
                state=state,
                provisioning_profile=copy.deepcopy(_PROFILE),
                shape=shape,
            ),
        )
    return system.id


def test_lists_callers_systems(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            await _seed_system(pool, allocation_id=alloc)
            await _seed_system(pool, allocation_id=alloc, state=SystemState.PROVISIONING)
            resp = await _list_systems(pool, _ctx())
        assert resp.object_id == "systems"
        assert resp.status == "ok"
        assert len(resp.items) == 2

    asyncio.run(_run())


def test_list_exposes_resource_kind(migrated_url: str) -> None:
    """systems.list rows carry resource_kind so an agent can match runs.bind (ADR-0169)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            await _seed_system(pool, allocation_id=alloc)
            resp = await _list_systems(pool, _ctx())
        assert resp.status == "ok" and len(resp.items) == 1
        assert resp.items[0].data["resource_kind"] == "local-libvirt"

    asyncio.run(_run())


def test_filter_by_allocation_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc_a = await _seed_allocation(pool, resource_id=res)
            alloc_b = await _seed_allocation(pool, resource_id=res)
            sys_a = await _seed_system(pool, allocation_id=alloc_a)
            await _seed_system(pool, allocation_id=alloc_b)
            resp = await _list_systems(pool, _ctx(), allocation_id=str(alloc_a))
        assert [r.object_id for r in resp.items] == [str(sys_a)]

    asyncio.run(_run())


def test_filter_by_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            ready = await _seed_system(pool, allocation_id=alloc, state=SystemState.READY)
            await _seed_system(pool, allocation_id=alloc, state=SystemState.PROVISIONING)
            resp = await _list_systems(pool, _ctx(), state="ready")
        assert [r.object_id for r in resp.items] == [str(ready)]

    asyncio.run(_run())


def test_filter_by_named_shape(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            large = await _seed_system(pool, allocation_id=alloc, shape="large")
            await _seed_system(pool, allocation_id=alloc, shape="small")
            await _seed_system(pool, allocation_id=alloc, shape=None)
            resp = await _list_systems(pool, _ctx(), shape="large")
        assert [r.object_id for r in resp.items] == [str(large)]

    asyncio.run(_run())


def test_shape_sentinel_returns_full_custom(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            custom = await _seed_system(pool, allocation_id=alloc, shape=None)
            await _seed_system(pool, allocation_id=alloc, shape="large")
            resp = await _list_systems(pool, _ctx(), shape=CUSTOM_SHAPE_SENTINEL)
        assert [r.object_id for r in resp.items] == [str(custom)]

    asyncio.run(_run())


def test_pcie_filter_matches_claimed_device(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            with_dev = await _seed_allocation(
                pool,
                resource_id=res,
                pcie_claim=[PCIeClaim(bdf="0000:01:00.0", vendor_id="8086", device_id="1572")],
            )
            without = await _seed_allocation(pool, resource_id=res)
            matched = await _seed_system(pool, allocation_id=with_dev)
            await _seed_system(pool, allocation_id=without)
            resp = await _list_systems(pool, _ctx(), pcie="8086:1572")
        assert [r.object_id for r in resp.items] == [str(matched)]

    asyncio.run(_run())


def test_pcie_filter_excludes_non_matching_device(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(
                pool,
                resource_id=res,
                pcie_claim=[PCIeClaim(bdf="0000:01:00.0", vendor_id="10de", device_id="2204")],
            )
            await _seed_system(pool, allocation_id=alloc)
            resp = await _list_systems(pool, _ctx(), pcie="8086:1572")
        assert resp.items == []

    asyncio.run(_run())


def test_malformed_pcie_spec_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_systems(pool, _ctx(), pcie="not-a-spec")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_empty_pcie_spec_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_systems(pool, _ctx(), pcie="   ")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_class_pcie_spec_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_systems(pool, _ctx(), pcie="class=02")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        # ADR-0174: a vendor/device-less match names its reason.
        assert resp.data["reason"] == "invalid_pcie_match"
        assert resp.detail is not None

    asyncio.run(_run())


def test_malformed_allocation_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_systems(pool, _ctx(), allocation_id="not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        # ADR-0174: actionable reason + non-null detail for the malformed-id parse failure.
        assert resp.data["reason"] == "invalid_uuid"
        assert resp.detail is not None and "not-a-uuid" in resp.detail

    asyncio.run(_run())


def test_unknown_state_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_systems(pool, _ctx(), state="bogus")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        # ADR-0174: an unknown state filter enumerates the accepted values.
        assert resp.data["reason"] == "invalid_state"
        assert "ready" in cast(list[str], resp.data["accepted_values"])

    asyncio.run(_run())


def test_ungranted_project_system_is_omitted(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            await _seed_budget_quota(pool, "other")
            res = await _seed_resource(pool)
            mine = await _seed_allocation(pool, project="proj", resource_id=res)
            theirs = await _seed_allocation(pool, project="other", resource_id=res)
            my_sys = await _seed_system(pool, allocation_id=mine, project="proj")
            await _seed_system(pool, allocation_id=theirs, project="other")
            resp = await _list_systems(pool, _ctx(projects=("proj",)))
        assert [r.object_id for r in resp.items] == [str(my_sys)]

    asyncio.run(_run())


def test_member_without_role_system_is_omitted(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, project="proj", resource_id=res)
            await _seed_system(pool, allocation_id=alloc, project="proj")
            # Member of "proj" but with no role granted on it (roles claim omits it).
            ctx = RequestContext(
                principal="user-1", agent_session="s", projects=("proj",), roles={}
            )
            resp = await _list_systems(pool, ctx)
        assert resp.items == []

    asyncio.run(_run())


def test_no_viewer_projects_returns_empty_collection(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = RequestContext(principal="user-1", agent_session="s", projects=(), roles={})
            resp = await _list_systems(pool, ctx)
        assert resp.status == "ok"
        assert resp.items == []

    asyncio.run(_run())


def test_cap_applies_after_filters_no_undercount(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            without = await _seed_allocation(pool, resource_id=res)
            with_dev = await _seed_allocation(
                pool,
                resource_id=res,
                pcie_claim=[PCIeClaim(bdf="0000:01:00.0", vendor_id="8086", device_id="1572")],
            )
            # The matching System is the OLDEST (would fall off a cap-then-filter page).
            matched = await _seed_system(
                pool, allocation_id=with_dev, created_at=datetime(2025, 1, 1, tzinfo=UTC)
            )
            for i in range(5):
                await _seed_system(
                    pool,
                    allocation_id=without,
                    created_at=datetime(2026, 6, i + 1, tzinfo=UTC),
                )
            resp = await _list_systems(pool, _ctx(), pcie="8086:1572", limit=3)
        assert [r.object_id for r in resp.items] == [str(matched)]

    asyncio.run(_run())


def test_limit_clamps_to_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            for i in range(3):
                await _seed_system(
                    pool, allocation_id=alloc, created_at=datetime(2026, 6, i + 1, tzinfo=UTC)
                )
            resp_lo = await _list_systems(pool, _ctx(), limit=0)
            resp_hi = await _list_systems(pool, _ctx(), limit=10_000)
        assert len(resp_lo.items) == 1  # min clamp to 1
        assert len(resp_hi.items) == 3  # max clamp does not error; returns all

    asyncio.run(_run())


@pytest.mark.parametrize("bad_state", ["", "torn", "READY"])
def test_state_filter_rejects_invalid_values(migrated_url: str, bad_state: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_systems(pool, _ctx(), state=bad_state)
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_list_surfaces_placement_no_per_item_keys(migrated_url: str) -> None:
    """systems.list rows carry resource_id + allocation_id, but no get-only N+1 keys."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            await _seed_system(pool, allocation_id=alloc)
            resp = await _list_systems(pool, _ctx())
        assert resp.status == "ok" and len(resp.items) == 1
        item = resp.items[0]
        assert item.data["resource_id"] == str(res)
        assert item.data["allocation_id"] == str(alloc)
        assert "active_run" not in item.data
        assert "active_debug_session_ids" not in item.data

    asyncio.run(_run())


def test_pagination_drains_distinct_timestamps(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            for i in range(5):
                await _seed_system(
                    pool, allocation_id=alloc, created_at=datetime(2026, 6, i + 1, tzinfo=UTC)
                )
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(10):
                page = await _list_systems(pool, _ctx(), limit=2, cursor=cursor)
                seen.extend(item.object_id for item in page.items)
                if not page.data["truncated"]:
                    break
                cursor = cast(str, page.data["next_cursor"])
        assert len(seen) == 5
        assert len(set(seen)) == 5

    asyncio.run(_run())


def test_pagination_drains_tied_timestamps(migrated_url: str) -> None:
    # Every System shares one created_at microsecond; the id DESC tiebreaker must keep the
    # page boundary total so the cursor never skips or repeats across the tie (ADR-0192).
    async def _run() -> None:
        tie = datetime(2026, 6, 1, tzinfo=UTC)
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            for _ in range(5):
                await _seed_system(pool, allocation_id=alloc, created_at=tie)
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(10):
                page = await _list_systems(pool, _ctx(), limit=2, cursor=cursor)
                seen.extend(item.object_id for item in page.items)
                if not page.data["truncated"]:
                    break
                cursor = cast(str, page.data["next_cursor"])
        assert len(seen) == 5
        assert len(set(seen)) == 5

    asyncio.run(_run())


def test_pagination_no_truncation_at_exactly_limit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            res = await _seed_resource(pool)
            alloc = await _seed_allocation(pool, resource_id=res)
            for i in range(2):
                await _seed_system(
                    pool, allocation_id=alloc, created_at=datetime(2026, 6, i + 1, tzinfo=UTC)
                )
            resp = await _list_systems(pool, _ctx(), limit=2)
        assert resp.data["truncated"] is False
        assert resp.data["next_cursor"] is None

    asyncio.run(_run())


def test_pagination_malformed_cursor_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_budget_quota(pool, "proj")
            resp = await _list_systems(pool, _ctx(), limit=2, cursor="!!!")
        assert resp.status == "error"
        assert resp.data["reason"] == "invalid_cursor"

    asyncio.run(_run())
