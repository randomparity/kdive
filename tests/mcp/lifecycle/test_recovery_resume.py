"""Resume-from-read-tools integration test (#568, ADR-0180).

Verifies that the three read tools — ``allocations.get``, ``systems.get``, and ``runs.get``
— surface the ids an agent needs to resume a lifecycle workflow from a cold start:
  - ``allocations.get`` → ``resource_id`` (for ``systems.provision``)
  - ``systems.get`` → ``allocation_id`` + ``resource_kind`` (for ``runs.bind`` / install)
  - ``runs.get`` → ``system_id`` + ``investigation_id``

Seeds a full spine: resource → granted allocation → ready system → bound + succeeded run.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import (
    ALLOCATIONS,
    BUDGETS,
    INVESTIGATIONS,
    QUOTAS,
    RESOURCES,
    RUNS,
    SYSTEMS,
)
from kdive.domain.accounting import Budget, Quota
from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle import Allocation, Investigation, Run, System
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle.allocations.view import get_allocation
from kdive.mcp.tools.lifecycle.runs.view import get_run
from kdive.mcp.tools.lifecycle.systems.view import get_system
from kdive.security.authz.rbac import Role
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROVISIONING_PROFILE: dict[str, Any] = {
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

_BUILD_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org#v6.9",
    "config": {"kind": "local", "path": "/configs/kdump.config"},
}


def _ctx(
    *,
    projects: tuple[str, ...] = ("proj",),
    role: Role = Role.VIEWER,
) -> RequestContext:
    return RequestContext(
        principal="user-1",
        agent_session="s",
        projects=projects,
        roles={p: role for p in projects},
    )


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
                project=project,
                limit_kcu=Decimal("1000000"),
                spent_kcu=Decimal(0),
                updated_at=_DT,
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
    resource_id: UUID,
    project: str = "proj",
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
                pcie_claim=[],
            ),
        )
    return alloc.id


async def _seed_system(
    pool: AsyncConnectionPool,
    *,
    allocation_id: UUID,
    state: SystemState = SystemState.READY,
    project: str = "proj",
) -> UUID:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=allocation_id,
                state=state,
                provisioning_profile=copy.deepcopy(_PROVISIONING_PROFILE),
            ),
        )
    return system.id


async def _seed_investigation(pool: AsyncConnectionPool, *, project: str = "proj") -> UUID:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                title="seeded",
                state=InvestigationState.OPEN,
            ),
        )
    return inv.id


async def _seed_run(
    pool: AsyncConnectionPool,
    *,
    system_id: UUID,
    investigation_id: UUID,
    state: RunState = RunState.SUCCEEDED,
    project: str = "proj",
) -> UUID:
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                investigation_id=investigation_id,
                system_id=system_id,
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=state,
                build_profile=copy.deepcopy(_BUILD_PROFILE),
            ),
        )
    return run.id


def test_resume_from_read_tools(migrated_url: str) -> None:
    """All three read tools surface the ids an agent needs to resume a lifecycle workflow."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # Spine: resource → granted allocation → ready system → bound + succeeded run.
            await _seed_budget_quota(pool, "proj")
            resource_id = await _seed_resource(pool)
            alloc_id = await _seed_allocation(pool, resource_id=resource_id)
            system_id = await _seed_system(pool, allocation_id=alloc_id, state=SystemState.READY)
            inv_id = await _seed_investigation(pool)
            run_id = await _seed_run(pool, system_id=system_id, investigation_id=inv_id)

            ctx = _ctx()
            alloc_resp = await get_allocation(pool, ctx, str(alloc_id))
            sys_resp = await get_system(pool, ctx, str(system_id))
            run_resp = await get_run(pool, ctx, str(run_id), resolver=provider_resolver())

        # systems.provision needs the granted resource id:
        assert alloc_resp.data["resource_id"] is not None
        assert alloc_resp.data["resource_id"] == str(resource_id)

        # runs.bind / install need the system + its allocation + run's investigation:
        assert sys_resp.data["allocation_id"] is not None
        assert sys_resp.data["resource_kind"] is not None
        assert run_resp.data["system_id"] == str(system_id)
        assert run_resp.data["investigation_id"] is not None
        assert run_resp.data["investigation_id"] == str(inv_id)

    asyncio.run(_run())
