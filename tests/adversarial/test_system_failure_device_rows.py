"""Adversarial: which failed System operations move `attached` device rows to `lost` (ADR-0823).

`devices_replaced` defaults to `False`; only `reprovision_handler` passes `True`, because only
reprovision recreates the domain without the hot-plugged devices. A failed provision never had
devices to lose, so its rows stay `attached`. These tests drive the real handlers against a real
database so a flipped default, or a second caller passing `True`, turns them red. Provision
reaches `_record_system_failure` through `_execute_system_lifecycle_call`, which passes its own
default explicitly, so that is the default the provision case pins; `_record_system_failure`'s own
default is reached only by `restore_handler`, which this module does not cover.

`teardown_handler` never reaches `_record_system_failure` (`tearing_down -> failed` is not an
edge) and reclaims the device ledger itself before the provider call, so a failed teardown
must leave no `lost` rows; that behavior is pinned here too.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEM_DEVICES, SYSTEMS, devices_for_system
from kdive.domain.capacity.state import AllocationState, SystemDeviceState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System, SystemDevice
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers import systems as systems_handlers
from kdive.jobs.payloads import Authorizing, ReprovisionPayload, SystemPayload, TeardownPayload
from tests.adversarial.conftest import seed_allocation, seed_resource
from tests.mcp.systems_support import provider_resolver
from tests.support.object_store import INERT_OBJECT_STORE

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
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
_ACTOR = Authorizing(principal="alice", agent_session="s", project="proj")


class _FailingProvisioner:
    """Every provider call raises a categorized error."""

    def _boom(self) -> None:
        raise CategorizedError("provider refused", category=ErrorCategory.PROVISIONING_FAILURE)

    def provision(self, *args: object, **kwargs: object) -> str:
        self._boom()
        raise AssertionError("unreachable")

    def reprovision(self, *args: object, **kwargs: object) -> str:
        self._boom()
        raise AssertionError("unreachable")

    def teardown(self, domain_name: str) -> None:
        del domain_name
        self._boom()

    def read_resolved_cpu(self, system_id: object) -> None:
        del system_id


@asynccontextmanager
async def _pool(url: str) -> AsyncGenerator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system_with_attached_device(pool: AsyncConnectionPool, state: SystemState) -> UUID:
    async with pool.connection() as conn:
        resource = await seed_resource(conn, cap=4)
        allocation = await seed_allocation(conn, resource.id, AllocationState.ACTIVE)
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                agent_session="s",
                project="proj",
                allocation_id=allocation.id,
                state=state,
                provisioning_profile=_PROFILE,
            ),
        )
        await SYSTEM_DEVICES.insert(
            conn,
            SystemDevice(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                system_id=system.id,
                kind="nic",
                state=SystemDeviceState.ATTACHED,
                network="default",
            ),
        )
    return system.id


async def _enqueue(pool: AsyncConnectionPool, kind: JobKind, payload: SystemPayload) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(conn, kind, payload, _ACTOR, f"{payload.system_id}:{kind.value}")


async def _device_states(pool: AsyncConnectionPool, system_id: UUID) -> list[SystemDeviceState]:
    async with pool.connection() as conn:
        return [row.state for row in await devices_for_system(conn, system_id)]


async def _system_state(pool: AsyncConnectionPool, system_id: UUID) -> SystemState:
    async with pool.connection() as conn:
        system = await SYSTEMS.get(conn, system_id)
    assert system is not None
    return system.state


def test_failed_provision_leaves_attached_devices_attached(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sid = await _seed_system_with_attached_device(pool, SystemState.PROVISIONING)
            job = await _enqueue(pool, JobKind.PROVISION, SystemPayload(system_id=str(sid)))
            async with pool.connection() as conn:
                await conn.set_autocommit(True)
                with pytest.raises(CategorizedError, match="provider refused"):
                    await systems_handlers.provision_handler(
                        conn, job, resolver=provider_resolver(provisioner=_FailingProvisioner())
                    )
            assert await _system_state(pool, sid) is SystemState.FAILED
            assert await _device_states(pool, sid) == [SystemDeviceState.ATTACHED]

    asyncio.run(_run())


def test_failed_reprovision_marks_attached_devices_lost(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sid = await _seed_system_with_attached_device(pool, SystemState.REPROVISIONING)
            job = await _enqueue(
                pool,
                JobKind.REPROVISION,
                ReprovisionPayload(system_id=str(sid), profile_digest="d"),
            )
            async with pool.connection() as conn:
                await conn.set_autocommit(True)
                with pytest.raises(CategorizedError, match="provider refused"):
                    await systems_handlers.reprovision_handler(
                        conn, job, resolver=provider_resolver(provisioner=_FailingProvisioner())
                    )
            assert await _system_state(pool, sid) is SystemState.FAILED
            assert await _device_states(pool, sid) == [SystemDeviceState.LOST]

    asyncio.run(_run())


def test_failed_teardown_never_marks_devices_lost(migrated_url: str) -> None:
    """Teardown reclaims the ledger before the provider call, so no row survives to go `lost`."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sid = await _seed_system_with_attached_device(pool, SystemState.READY)
            job = await _enqueue(pool, JobKind.TEARDOWN, TeardownPayload(system_id=str(sid)))
            async with pool.connection() as conn:
                await conn.set_autocommit(True)
                with pytest.raises(CategorizedError, match="provider refused"):
                    await systems_handlers.teardown_handler(
                        conn,
                        job,
                        resolver=provider_resolver(provisioner=_FailingProvisioner()),
                        artifact_store=INERT_OBJECT_STORE,
                    )
            assert await _system_state(pool, sid) is SystemState.TEARING_DOWN
            assert await _device_states(pool, sid) == []

    asyncio.run(_run())
