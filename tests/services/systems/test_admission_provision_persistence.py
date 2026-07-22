"""Persistence, audit, quota, and recycle-failure assertions for systems.provision admission.

The transport-bound suite pins the deadline mechanics; these pin the *content* the mutating
segment writes: the System fields ``_insert_system_and_activate`` persists, both audit
transitions it records, the enqueued provision job, the fail-closed ``max_concurrent_systems``
quota boundary, and the recycle-required failure returned for a non-recoverable existing System.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import ErrorCategory
from kdive.security.audit import args_digest
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
    granted_allocation as _granted_allocation,
)
from tests.mcp.systems_support import (
    pool as _pool,
)
from tests.mcp.systems_support import (
    provisioning_profile as _profile,
)


def _admission() -> SystemAdmission:
    return SystemAdmission(_TEST_PROFILE_POLICY, _TEST_COMPONENT_SOURCES, lambda _rootfs: None)


async def _provision(
    admission: SystemAdmission,
    pool: AsyncConnectionPool,
    alloc_id: str,
    *,
    label: str | None = None,
) -> AdmissionFailure | ProvisionJobAdmitted:
    result = await admission.create_for_allocation(
        pool,
        _ctx(),
        CreateSystemRequest(
            allocation_id=UUID(alloc_id), profile=_profile(), mode="provision", label=label
        ),
    )
    assert isinstance(result, AdmissionFailure | ProvisionJobAdmitted)
    return result


def test_provision_persists_system_fields_audit_and_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool, shape="rpt-small")
            result = await _provision(_admission(), pool, alloc_id, label="my-label")
            assert isinstance(result, ProvisionJobAdmitted)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT * FROM systems WHERE allocation_id = %s", (UUID(alloc_id),)
                )
                system = await cur.fetchone()
                assert system is not None
                await cur.execute(
                    "SELECT tool, object_kind, transition, args_digest FROM audit_log "
                    "WHERE object_id = %s",
                    (system["id"],),
                )
                system_audit = await cur.fetchall()
                await cur.execute(
                    "SELECT object_kind, transition, args_digest FROM audit_log "
                    "WHERE object_id = %s",
                    (UUID(alloc_id),),
                )
                alloc_audit = await cur.fetchall()
        # The mutating segment stamps the request identity, the allocation binding, and the
        # client label onto the new PROVISIONING System.
        assert system["state"] == SystemState.PROVISIONING.value
        assert system["principal"] == "user-1"
        assert system["agent_session"] == "s"
        assert system["project"] == "proj"
        assert system["allocation_id"] == UUID(alloc_id)
        assert system["shape"] == "rpt-small"
        assert system["label"] == "my-label"
        # The admitted job is linked back to the System.
        assert result.system_id == system["id"]
        assert result.job is not None
        # Both audit rows are keyed on the allocation and carry the expected transitions.
        expected_digest = args_digest({"allocation_id": alloc_id})
        sys_ev = next(r for r in system_audit if r["object_kind"] == "systems")
        assert sys_ev["tool"] == "systems.provision"
        assert sys_ev["transition"] == "->provisioning"
        assert sys_ev["args_digest"] == expected_digest
        alloc_ev = next(r for r in alloc_audit if r["object_kind"] == "allocations")
        assert alloc_ev["transition"] == "granted->active"
        assert alloc_ev["args_digest"] == expected_digest

    asyncio.run(_run())


def test_provision_denied_at_system_quota_boundary(migrated_url: str) -> None:
    async def _run() -> tuple[AdmissionFailure | ProvisionJobAdmitted, str]:
        async with _pool(migrated_url) as pool:
            # A per-project cap of one: the first provision fills it, a second is denied at the
            # boundary (count == cap admits nothing more — strictly-under is required).
            first_alloc = await _granted_allocation(pool, systems_quota=1)
            first = await _provision(_admission(), pool, first_alloc)
            assert isinstance(first, ProvisionJobAdmitted)
            second_alloc = await _granted_allocation(pool, systems_quota=1)
            return await _provision(_admission(), pool, second_alloc), second_alloc

    result, second_alloc = asyncio.run(_run())
    assert isinstance(result, AdmissionFailure)
    assert result.subject_id == UUID(second_alloc)
    assert result.category is ErrorCategory.QUOTA_EXCEEDED
    assert result.reason is AdmissionFailureReason.QUOTA_EXCEEDED
    assert result.recovery is AdmissionRecovery.INSPECT_SYSTEMS_AND_ALLOCATIONS


def test_provision_on_unrecoverable_system_requires_recycle(migrated_url: str) -> None:
    async def _run() -> tuple[AdmissionFailure | ProvisionJobAdmitted, str]:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            system_id = await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            return await _provision(_admission(), pool, alloc_id), system_id

    result, system_id = asyncio.run(_run())
    assert isinstance(result, AdmissionFailure)
    assert result.subject_id == UUID(system_id)
    assert result.category is ErrorCategory.CONFIGURATION_ERROR
    assert result.reason is AdmissionFailureReason.SYSTEM_RECYCLE_REQUIRED
    assert result.current_status == SystemState.TORN_DOWN.value
    assert result.recovery is AdmissionRecovery.RECYCLE_ALLOCATION
    assert result.failure_message  # non-empty recycle guidance
