"""Shared debug-session test builders and database seeders."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, DEBUG_SESSIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.records import Allocation, DebugSession, Investigation, Run, System
from kdive.mcp.auth import RequestContext
from kdive.providers.core.resource_registration import register_discovered_resource
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.ports.lifecycle import TransportHandleData, TransportHandleKind
from kdive.security.authz.rbac import Role
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)

PROFILE_POLICY = LocalLibvirtProfilePolicy()

PROFILE: dict[str, Any] = {
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
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def request_context(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    """Build the shared debug-test request context."""
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    """Open and close the debug-test connection pool."""
    connection_pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await connection_pool.open()
    try:
        yield connection_pool
    finally:
        await connection_pool.close()


async def granted_allocation(pool: AsyncConnectionPool) -> str:
    """Seed the shared local-libvirt Resource and granted Allocation graph."""
    discovery = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=2,
    )
    async with pool.connection() as conn:
        resource = await register_discovered_resource(
            conn,
            discovery.list_resources()[0],
            pool="local-libvirt",
            cost_class="local",
        )
        allocation = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
                principal="user-1",
                project="proj",
                resource_id=resource.id,
                state=AllocationState.GRANTED,
            ),
        )
    return str(allocation.id)


async def seed_system(pool: AsyncConnectionPool, allocation_id: str, state: SystemState) -> str:
    """Seed a System with the shared profile and requested state."""
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
                principal="user-1",
                project="proj",
                allocation_id=UUID(allocation_id),
                state=state,
                provisioning_profile=copy.deepcopy(PROFILE),
                domain_name="kdive-x",
            ),
        )
    return str(system.id)


async def seed_run(
    pool: AsyncConnectionPool,
    system_id: str,
    *,
    state: RunState = RunState.SUCCEEDED,
    booted: bool = True,
    boot_result: dict[str, Any] | None = None,
) -> str:
    """Seed an Investigation and Run, including a succeeded boot by default."""
    async with pool.connection() as conn:
        investigation = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
                principal="user-1",
                project="proj",
                title="t",
                state=InvestigationState.ACTIVE,
            ),
        )
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
                principal="user-1",
                project="proj",
                investigation_id=investigation.id,
                system_id=UUID(system_id),
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=state,
                build_profile={},
            ),
        )
        if booted:
            await conn.execute(
                "INSERT INTO run_steps (run_id, step, state, result) "
                "VALUES (%s, 'boot', 'succeeded', %s)",
                (run.id, Jsonb({} if boot_result is None else boot_result)),
            )
    return str(run.id)


async def seed_session(
    pool: AsyncConnectionPool,
    run_id: str,
    state: DebugSessionState,
    *,
    transport: str = "gdbstub",
) -> str:
    """Seed a DebugSession with the requested state and transport."""
    port = 22 if transport == "drgn-live" else 1234
    handle_kind = cast(TransportHandleKind, transport)
    async with pool.connection() as conn:
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
                principal="user-1",
                project="proj",
                run_id=UUID(run_id),
                state=state,
                transport=transport,
                transport_handle=TransportHandleData(
                    kind=handle_kind,
                    host="127.0.0.1",
                    port=port,
                ).encode(),
            ),
        )
    return str(session.id)


async def seed_live_session(pool: AsyncConnectionPool, *, state: DebugSessionState) -> str:
    """Seed the full attachable graph and return its DebugSession identifier."""
    allocation_id = await granted_allocation(pool)
    system_id = await seed_system(pool, allocation_id, SystemState.READY)
    run_id = await seed_run(pool, system_id)
    return await seed_session(pool, run_id, state)
