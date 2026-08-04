"""Shared support helpers for Run lifecycle tests."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, Investigation, System
from kdive.domain.pcie import PCIeClaim
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle.runs.create import (
    RunCreateRequest,
    RunReuseRequirementInput,
    create_run,
)
from kdive.mcp.tools.lifecycle.runs.steps import install_run
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.security.authz.rbac import Role
from tests.mcp.systems_support import provider_resolver

TEST_DT = datetime(2026, 1, 1, tzinfo=UTC)
BUILD_PROFILE: dict[str, Any] = {"schema_version": 1}
LOCAL_PROFILE_POLICY = LocalLibvirtProfilePolicy()


def build_profile() -> dict[str, Any]:
    """Return an isolated copy of the minimal Run build profile."""
    return copy.deepcopy(BUILD_PROFILE)


def profile_dump(**local_libvirt: Any) -> dict[str, Any]:
    """Return the canonical local-libvirt provisioning profile used by Run tests."""
    section: dict[str, Any] = {"rootfs": {"kind": "local", "path": "/img"}}
    section.update(local_libvirt)
    return ProvisioningProfile.model_validate(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org#v6.9",
            "provider": {"local-libvirt": section},
        }
    ).model_dump(by_alias=True)


def ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    """Return the standard Run-test request context."""
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    """Open a bounded async connection pool for a test scenario."""
    conn_pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await conn_pool.open()
    try:
        yield conn_pool
    finally:
        await conn_pool.close()


async def seed_system(
    conn_pool: AsyncConnectionPool,
    *,
    system_state: SystemState = SystemState.READY,
    alloc_state: AllocationState = AllocationState.ACTIVE,
    project: str = "proj",
    provisioning_profile: dict[str, Any] | None = None,
    requested_vcpus: int | None = None,
    requested_memory_gb: int | None = None,
    requested_disk_gb: int | None = None,
    pcie_claim: list[PCIeClaim] | None = None,
    lease_expiry: datetime | None = None,
) -> str:
    """Insert a Resource, Allocation, and System without quota or budget state."""
    async with conn_pool.connection() as conn:
        resource = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="local-libvirt",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        allocation = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project=project,
                resource_id=resource.id,
                state=alloc_state,
                requested_vcpus=requested_vcpus,
                requested_memory_gb=requested_memory_gb,
                requested_disk_gb=requested_disk_gb,
                pcie_claim=pcie_claim or [],
                lease_expiry=lease_expiry,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project=project,
                allocation_id=allocation.id,
                state=system_state,
                provisioning_profile=(
                    provisioning_profile if provisioning_profile is not None else profile_dump()
                ),
            ),
        )
    return str(system.id)


async def seed_investigation(
    conn_pool: AsyncConnectionPool,
    *,
    state: InvestigationState = InvestigationState.OPEN,
    project: str = "proj",
) -> str:
    """Insert an Investigation directly and return its id."""
    async with conn_pool.connection() as conn:
        investigation = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project=project,
                title="seeded",
                state=state,
            ),
        )
    return str(investigation.id)


async def create(
    conn_pool: AsyncConnectionPool,
    request_ctx: RequestContext,
    investigation_id: str,
    system_id: str,
    *,
    profile: dict[str, Any] | None = None,
    reuse_requirement: RunReuseRequirementInput | None = None,
    idempotency_key: str | None = None,
    label: str | None = None,
    build_ref: str | None = None,
):
    """Call the Run create handler with the standard provider resolver."""
    return await create_run(
        conn_pool,
        request_ctx,
        RunCreateRequest(
            investigation_id=investigation_id,
            system_id=system_id,
            build_profile=profile or build_profile(),
            reuse_requirement=reuse_requirement,
            label=label,
            build_ref=build_ref,
        ),
        resolver=provider_resolver(),
        idempotency_key=idempotency_key,
    )


async def seed_investigation_build(conn_pool: AsyncConnectionPool, investigation_id: str) -> str:
    """Insert an active reusable Investigation build and return its reference."""
    digest = "b" * 64
    generation = uuid4()
    build_ref = f"{digest}.{generation}"
    expires_at = datetime.now(UTC) + timedelta(days=7)
    async with conn_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO investigation_builds "
            "(investigation_id, generation, build_ref, content_digest, canonical_document, "
            "build_result, artifacts, target_kind, build_profile, state, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'local-libvirt', %s, 'active', %s)",
            (
                investigation_id,
                generation,
                build_ref,
                digest,
                Jsonb({"version": 1}),
                Jsonb({"kernel_ref": "build/kernel", "build_id": "id"}),
                Jsonb({}),
                Jsonb({"schema_version": 1, "arch": "x86_64"}),
                expires_at,
            ),
        )
    return build_ref


async def install(conn_pool: AsyncConnectionPool, request_ctx: RequestContext, run_id: str) -> Any:
    """Call the Run install handler with the local-libvirt profile policy."""
    return await install_run(
        conn_pool,
        request_ctx,
        run_id,
        resolver=provider_resolver(profile_policy=LOCAL_PROFILE_POLICY),
    )
