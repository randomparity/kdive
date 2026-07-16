"""The post-provision resolved_cpu write is local-only and state-guarded (#1227, ADR-0369).

``_persist_local_resolved_cpu`` reads the running local domain's CPU and persists it at the READY
boundary. Remote/fault provisioners are skipped (they keep the ADR-0368 mint snapshot / have no
domain); a local write lands only while the System is in ``{PROVISIONING, READY}``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, System
from kdive.jobs.handlers.systems import _persist_local_resolved_cpu
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.serialization import JsonValue

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_RESOLVED: dict[str, JsonValue] = {
    "model": "SapphireRapids",
    "arch": "x86_64",
    "baseline_level": "x86-64-v4",
}


class _FakeLocalProvisioner(LocalLibvirtProvisioning):
    """A LocalLibvirtProvisioning (so ``isinstance`` holds) whose read is stubbed — no libvirt."""

    def __init__(self, resolved: dict[str, JsonValue] | None) -> None:
        self._resolved = resolved  # deliberately skips the heavy base __init__

    def read_resolved_cpu(self, system_id: UUID) -> dict[str, JsonValue] | None:
        return self._resolved


def _runtime(provisioner: object) -> ProviderRuntime:
    unused = cast(Any, object())
    return ProviderRuntime(
        profile_policy=unused,
        provisioner=cast(Any, provisioner),
        installer=unused,
        booter=unused,
        connector=unused,
        controller=unused,
        retriever=unused,
        crash_postmortem=unused,
        vmcore_introspector=unused,
        live_introspector=unused,
    )


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=False)


def _resource() -> Resource:
    return Resource(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=ResourceKind.LOCAL_LIBVIRT,
        capabilities={},
        pool="default",
        cost_class="standard",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
    )


def _allocation(resource_id: UUID) -> Allocation:
    return Allocation(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="alice",
        project="proj",
        resource_id=resource_id,
        state=AllocationState.GRANTED,
    )


def _system(allocation_id: UUID) -> System:
    return System(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="alice",
        project="proj",
        allocation_id=allocation_id,
        state=SystemState.DEFINED,
        provisioning_profile={"k": "v"},
    )


async def _seed_ready_system(conn: psycopg.AsyncConnection) -> System:
    res = await RESOURCES.insert(conn, _resource())
    alloc = await ALLOCATIONS.insert(conn, _allocation(res.id))
    sysm = await SYSTEMS.insert(conn, _system(alloc.id))
    await SYSTEMS.update_state(conn, sysm.id, SystemState.PROVISIONING)
    return await SYSTEMS.update_state(conn, sysm.id, SystemState.READY)


def test_local_provisioner_persists_resolved_cpu(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            sysm = await _seed_ready_system(conn)
            await _persist_local_resolved_cpu(
                conn, sysm, _runtime(_FakeLocalProvisioner(_RESOLVED))
            )
            reloaded = await SYSTEMS.get(conn, sysm.id)
            assert reloaded is not None
            assert reloaded.resolved_cpu == _RESOLVED

    asyncio.run(_run())


def test_local_read_failure_persists_null(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            sysm = await _seed_ready_system(conn)
            await _persist_local_resolved_cpu(conn, sysm, _runtime(_FakeLocalProvisioner(None)))
            reloaded = await SYSTEMS.get(conn, sysm.id)
            assert reloaded is not None
            assert reloaded.resolved_cpu is None

    asyncio.run(_run())


def test_non_local_provisioner_writes_nothing(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            sysm = await _seed_ready_system(conn)
            await _persist_local_resolved_cpu(conn, sysm, _runtime(object()))  # not local
            reloaded = await SYSTEMS.get(conn, sysm.id)
            assert reloaded is not None
            assert reloaded.resolved_cpu is None

    asyncio.run(_run())
