"""Each caller of the authority teardown admission states its own recycle policy (ADR-0435).

Automatic callers pass ``NEVER`` so a ``failed`` authority teardown is not restarted every
sweep (#3530); operator-initiated callers pass ``FAILED`` so a retry restarts it, and
``systems.teardown`` passes ``FAILED_OR_LAPSED_EXHAUSTED`` so it also restarts a final attempt
whose lease lapsed without a receipt (#3617). The service applies whatever the caller passes, so
only a per-caller assertion catches a flipped policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import AllocationState, SystemState
from kdive.jobs import queue
from kdive.jobs import service_operations as job_service_operations
from kdive.mcp.tools import jobs as jobs_tools
from kdive.mcp.tools.lifecycle.systems import admin as systems_admin
from kdive.mcp.tools.lifecycle.systems.provision import SystemProvisionHandlers
from kdive.mcp.tools.ops.security import breakglass
from kdive.reconciler.repairs import systems as reconciler_systems
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role
from tests.mcp import systems_support
from tests.reconciler.conftest import connect, seed_system

_NEVER = queue.JobRecyclePolicy.NEVER
_FAILED = queue.JobRecyclePolicy.FAILED
_FAILED_OR_LAPSED = queue.JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED
_DIGEST = "sha256:" + "a" * 64
_ROOT = {
    "root_spec": {
        "schema": "root-spec-v1",
        "architecture": "x86_64",
        "root": "UUID=authority-root",
        "arguments": ["root=UUID=authority-root", "rootfstype=xfs"],
        "authority": "stage-inspection",
        "source": {"kind": "staged-image", "identity": _DIGEST},
    }
}

Recorded = list[queue.JobRecyclePolicy]
Driver = Callable[[str], Awaitable[None]]


def _record_recycle(monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str) -> Recorded:
    """Wrap ``module.name`` so every call's ``recycle`` argument is recorded, then delegates."""
    recorded: Recorded = []
    original = getattr(module, name)

    async def spy(*args: Any, **kwargs: Any) -> object:
        recorded.append(kwargs["recycle"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, name, spy)
    return recorded


async def _ordinary_system(url: str, *, alloc_state: AllocationState) -> UUID:
    async with await connect(url) as conn:
        return await seed_system(conn, system_state=SystemState.READY, alloc_state=alloc_state)


async def _provision_preactivation_system(pool: AsyncConnectionPool) -> str:
    """Admit an authority-owned System that has not been activated yet."""
    handlers = SystemProvisionHandlers(
        systems_support.TEST_PROFILE_POLICY,
        systems_support.TEST_COMPONENT_SOURCES,
        lambda _root: None,
        authority_route=lambda _kind, _name: "authority-a",
    )
    allocation_id = await systems_support.granted_allocation(pool)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE resources SET name='host-a' WHERE id=("
            "SELECT resource_id FROM allocations WHERE id=%s)",
            (allocation_id,),
        )
        await conn.execute(
            "INSERT INTO image_catalog "
            "(id,provider,name,arch,format,root_device,object_key,digest,provenance,"
            "visibility,state) VALUES "
            "(%s,'local-libvirt','authority-base','x86_64','qcow2','/dev/vda',"
            "'authority-base.qcow2',%s,%s,'public','registered')",
            (uuid4(), _DIGEST, Jsonb(_ROOT)),
        )
    profile = systems_support.provisioning_profile()
    profile["provider"]["local-libvirt"]["rootfs"]["sha256"] = _DIGEST
    provision = await handlers.provision_system(
        pool,
        systems_support.ctx(role=Role.CONTRIBUTOR),
        allocation_id=allocation_id,
        profile=profile,
    )
    return str(provision.object_id)


async def _reconciler_orphan_sweep(url: str) -> None:
    await _ordinary_system(url, alloc_state=AllocationState.RELEASED)
    async with await connect(url) as conn:
        assert await reconciler_systems.repair_orphaned_systems(conn) == 1


async def _job_service_teardown(url: str) -> None:
    system_id = await _ordinary_system(url, alloc_state=AllocationState.ACTIVE)
    async with systems_support.pool(url) as pool, pool.connection() as conn:
        await job_service_operations.JobOperations().enqueue_teardown(
            conn, systems_support.ctx(), project="proj", system_id=system_id
        )


async def _provision_cancel_fence(url: str) -> None:
    async with systems_support.pool(url) as pool:
        provision_job_id = await _provision_preactivation_system(pool)
        canceled = await jobs_tools.cancel_job(
            pool, systems_support.ctx(role=Role.CONTRIBUTOR), provision_job_id
        )
        assert canceled.status == "canceled"


async def _breakglass_teardown(url: str) -> None:
    system_id = await _ordinary_system(url, alloc_state=AllocationState.ACTIVE)
    admin = RequestContext(
        principal="ops-admin",
        agent_session="sess-admin",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )
    async with systems_support.pool(url) as pool:
        response = await breakglass.force_teardown(
            pool, admin, system_id=str(system_id), reason="pin recycle policy"
        )
        assert response.error_category is None


async def _operator_preactivation_teardown(url: str) -> None:
    async with systems_support.pool(url) as pool:
        await _provision_preactivation_system(pool)
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT id FROM systems")).fetchone()
        assert row is not None
        response = await systems_admin.teardown_system(
            pool, systems_support.ctx(role=Role.ADMIN), str(row[0])
        )
        assert response.status == "queued"


_CALL_SITES = [
    pytest.param(
        reconciler_systems,
        "enqueue_control_teardown",
        _reconciler_orphan_sweep,
        _NEVER,
        id="reconciler-orphan-sweep",
    ),
    pytest.param(
        job_service_operations,
        "enqueue_control_teardown",
        _job_service_teardown,
        _NEVER,
        id="job-service-teardown",
    ),
    pytest.param(
        jobs_tools,
        "enqueue_preactivation_teardown",
        _provision_cancel_fence,
        _NEVER,
        id="provision-cancel-fence",
    ),
    pytest.param(
        breakglass,
        "enqueue_control_teardown",
        _breakglass_teardown,
        _FAILED,
        id="breakglass-force-teardown",
    ),
    pytest.param(
        systems_admin,
        "enqueue_preactivation_teardown",
        _operator_preactivation_teardown,
        _FAILED_OR_LAPSED,
        id="operator-systems-teardown",
    ),
]


@pytest.mark.parametrize(("module", "name", "drive", "expected"), _CALL_SITES)
def test_call_site_passes_its_recycle_policy(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    name: str,
    drive: Driver,
    expected: queue.JobRecyclePolicy,
) -> None:
    recorded = _record_recycle(monkeypatch, module, name)

    asyncio.run(drive(migrated_url))

    assert recorded == [expected]
