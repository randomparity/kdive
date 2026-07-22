"""Host-binding and recycle-path admission fixtures (#1413, the #1400 deferred remainder).

The transport-bound and persistence suites drive ``systems.provision`` against the default
``FakeLibvirtConn`` resource, which advertises no ``guest_arches`` — so the host-derived
``accel`` resolution, the ``failed``-System recycle branch, and the enqueue dedup-key were
never exercised end-to-end under ``tests/services/systems/``. These fixtures seed a
capability-bearing resource and a dedup-keyed failed provision job so the mutating segment's
accel persistence, the ``is FAILED`` branch of ``_provision_create_response``, and the
``{allocation_id}:provision`` dedup key are all observable.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import ErrorCategory
from kdive.services.systems.admission import (
    AdmissionFailure,
    AdmissionFailureReason,
    AdmissionRecovery,
    CreateSystemRequest,
    ProvisionJobAdmitted,
    SystemAdmission,
)
from tests.mcp.lifecycle.test_systems_tools import _seed_system
from tests.mcp.systems_support import (
    TEST_COMPONENT_SOURCES as _TEST_COMPONENT_SOURCES,
)
from tests.mcp.systems_support import (
    TEST_PROFILE_POLICY as _TEST_PROFILE_POLICY,
)
from tests.mcp.systems_support import (
    ctx as _ctx,
)
from tests.mcp.systems_support import (
    enqueue_provision as _enqueue_provision,
)
from tests.mcp.systems_support import (
    granted_allocation as _granted_allocation,
)
from tests.mcp.systems_support import (
    pool as _pool,
)
from tests.mcp.systems_support import (
    provisioning_profile as _profile,
)

_X86_GUEST_ARCHES = {
    "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"},
    "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"},
}


def _admission() -> SystemAdmission:
    return SystemAdmission(_TEST_PROFILE_POLICY, _TEST_COMPONENT_SOURCES, lambda _rootfs: None)


async def _provision(
    pool: AsyncConnectionPool, alloc_id: str
) -> AdmissionFailure | ProvisionJobAdmitted:
    result = await _admission().create_for_allocation(
        pool,
        _ctx(),
        CreateSystemRequest(allocation_id=UUID(alloc_id), profile=_profile(), mode="provision"),
    )
    assert isinstance(result, AdmissionFailure | ProvisionJobAdmitted)
    return result


async def _set_resource_guest_arches(
    pool: AsyncConnectionPool, alloc_id: str, guest_arches: dict[str, Any]
) -> None:
    """Overwrite the backing Resource's ``guest_arches`` capability for the seeded allocation."""
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.get(conn, alloc_id)
        assert alloc is not None and alloc.resource_id is not None
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT capabilities FROM resources WHERE id = %s", (alloc.resource_id,)
            )
            row = await cur.fetchone()
            assert row is not None
            caps = dict(row["capabilities"])
            caps["guest_arches"] = guest_arches
            await cur.execute(
                "UPDATE resources SET capabilities = %s WHERE id = %s",
                (Jsonb(caps), alloc.resource_id),
            )


async def _system_row(pool: AsyncConnectionPool, alloc_id: str) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM systems WHERE allocation_id = %s", (UUID(alloc_id),))
        return await cur.fetchone()


async def _fail_provision_job(
    pool: AsyncConnectionPool, job_id: str, failure_context: dict[str, str]
) -> None:
    """Drive a seeded provision job to ``failed`` with the worker-redacted ``failure_context``."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE jobs SET state = 'failed', failure_context = %s WHERE id = %s",
            (Jsonb(failure_context), UUID(job_id)),
        )


def test_provision_records_accel_from_host_guest_arches(migrated_url: str) -> None:
    # The bound host advertises x86_64 as a KVM guest, so the mutating segment resolves and
    # persists accel="kvm" onto the new System. Under the default FakeLibvirtConn resource
    # (no guest_arches) accel is None, which never distinguishes the resolution mutants.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _X86_GUEST_ARCHES)
            result = await _provision(pool, alloc_id)
            assert isinstance(result, ProvisionJobAdmitted)
            row = await _system_row(pool, alloc_id)
        assert row is not None
        assert row["accel"] == "kvm"  # x86_64 is native (KVM) on this advertised host

    asyncio.run(_run())


def test_provision_enqueue_job_carries_allocation_dedup_key(migrated_url: str) -> None:
    # The enqueued provision job's dedup key is `{allocation_id}:provision` — the natural key a
    # retried provision idempotently dedups against. A mutant that drops the allocation id from
    # the key (`None:provision`) is caught here.
    async def _run() -> ProvisionJobAdmitted:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            result = await _provision(pool, alloc_id)
            assert isinstance(result, ProvisionJobAdmitted)
            assert result.job.dedup_key == f"{alloc_id}:provision"
            return result

    asyncio.run(_run())


def test_provision_on_failed_system_surfaces_failing_job_reason(migrated_url: str) -> None:
    # A `failed` System routes to the recycle-required failure that reads the original provision
    # reason from the failed provision job (keyed on `{alloc}:provision`). This is the only branch
    # that seeds `failing_job_id` and interpolates the original reason — the generic recycle branch
    # (torn_down, etc.) does neither, so the `is FAILED` discriminator is observable here.
    async def _run() -> tuple[AdmissionFailure | ProvisionJobAdmitted, str, str]:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            system_id = await _seed_system(pool, alloc_id, SystemState.FAILED)
            job = await _enqueue_provision(pool, system_id, alloc_id)
            await _fail_provision_job(
                pool,
                str(job.id),
                {
                    "failure_message": "kernel panic at boot",
                    "failure_detail_stage": "boot",
                },
            )
            return await _provision(pool, alloc_id), system_id, str(job.id)

    result, system_id, job_id = asyncio.run(_run())
    assert isinstance(result, AdmissionFailure)
    assert result.subject_id == UUID(system_id)
    assert result.category is ErrorCategory.CONFIGURATION_ERROR
    assert result.reason is AdmissionFailureReason.SYSTEM_RECYCLE_REQUIRED
    assert result.current_status == SystemState.FAILED.value
    assert result.recovery is AdmissionRecovery.RECYCLE_ALLOCATION
    assert result.failure_message is not None
    assert "kernel panic at boot" in result.failure_message  # original reason interpolated
    assert result.failure_details is not None
    assert result.failure_details["failing_job_id"] == job_id
    assert result.failure_details["failure_detail_stage"] == "boot"  # detail_ keys threaded


def test_provision_on_failed_system_without_failed_job_omits_reason(migrated_url: str) -> None:
    # A System can reach `failed` via `reprovisioning->failed`, leaving the original provision job
    # `succeeded`; that non-failed job must never be advertised as the failing one. With no failed
    # provision job the recycle failure carries the fixed guidance and no `failing_job_id`.
    async def _run() -> AdmissionFailure | ProvisionJobAdmitted:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _seed_system(pool, alloc_id, SystemState.FAILED)
            # A succeeded provision job on the same dedup key must not be treated as the failure.
            job = await _enqueue_provision(pool, str(UUID(int=0)), alloc_id)
            async with pool.connection() as conn:
                await conn.execute("UPDATE jobs SET state = 'succeeded' WHERE id = %s", (job.id,))
            return await _provision(pool, alloc_id)

    result = asyncio.run(_run())
    assert isinstance(result, AdmissionFailure)
    assert result.reason is AdmissionFailureReason.SYSTEM_RECYCLE_REQUIRED
    assert result.failure_message is not None
    assert "original reason" not in result.failure_message  # no reason from a non-failed job
    assert result.failure_details == {}  # no failing_job_id seeded
