"""System->Investigation binding admission assertions (ADR-0441 §2, #1502).

`systems.define`/`systems.provision` accept an optional `investigation_id`. A supplied binding is
validated at admission: it must name a non-terminal investigation in the System's own project, and
it is write-once (a later provision must match or omit it, never change it). Omitting it leaves the
classic allocation-only path unchanged.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import InvestigationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Investigation
from kdive.services.systems.admission import (
    AdmissionFailure,
    AdmissionResult,
    CreateSystemMode,
    CreateSystemRequest,
    DefinedSystemAdmitted,
    ProvisionJobAdmitted,
    SystemAdmission,
)
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

_DT_PROJECT = "proj"


def _admission() -> SystemAdmission:
    return SystemAdmission(_TEST_PROFILE_POLICY, _TEST_COMPONENT_SOURCES, lambda _rootfs: None)


async def _seed_investigation(
    conn: AsyncConnection,
    *,
    project: str = _DT_PROJECT,
    state: InvestigationState = InvestigationState.OPEN,
) -> UUID:
    inv = await INVESTIGATIONS.insert(
        conn,
        Investigation(
            id=uuid4(),
            created_at=_now(),
            updated_at=_now(),
            principal="user-1",
            project=project,
            title="inv",
            state=state,
        ),
    )
    return inv.id


def _now():
    from datetime import UTC, datetime

    return datetime(2026, 1, 1, tzinfo=UTC)


async def _create(
    admission: SystemAdmission,
    pool: AsyncConnectionPool,
    alloc_id: str,
    *,
    mode: CreateSystemMode = "provision",
    investigation_id: UUID | None = None,
) -> AdmissionResult:
    return await admission.create_for_allocation(
        pool,
        _ctx(),
        CreateSystemRequest(
            allocation_id=UUID(alloc_id),
            profile=_profile(),
            mode=mode,
            investigation_id=investigation_id,
        ),
    )


async def _row_investigation_id(pool: AsyncConnectionPool, alloc_id: str) -> UUID | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT investigation_id FROM systems WHERE allocation_id = %s", (UUID(alloc_id),)
        )
        row = await cur.fetchone()
    assert row is not None
    return row["investigation_id"]


def test_provision_persists_supplied_same_project_binding(migrated_url: str) -> None:
    async def _run() -> tuple[AdmissionResult, UUID | None, UUID]:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                inv_id = await _seed_investigation(conn)
            result = await _create(_admission(), pool, alloc_id, investigation_id=inv_id)
            return result, await _row_investigation_id(pool, alloc_id), inv_id

    result, stored, inv_id = asyncio.run(_run())
    assert isinstance(result, ProvisionJobAdmitted)
    assert stored == inv_id


def test_provision_without_binding_leaves_null(migrated_url: str) -> None:
    async def _run() -> tuple[AdmissionResult, UUID | None]:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            result = await _create(_admission(), pool, alloc_id)
            return result, await _row_investigation_id(pool, alloc_id)

    result, stored = asyncio.run(_run())
    assert isinstance(result, ProvisionJobAdmitted)
    assert stored is None


def test_provision_rejects_terminal_investigation(migrated_url: str) -> None:
    async def _run() -> AdmissionResult:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                inv_id = await _seed_investigation(conn, state=InvestigationState.CLOSED)
            return await _create(_admission(), pool, alloc_id, investigation_id=inv_id)

    result = asyncio.run(_run())
    assert isinstance(result, AdmissionFailure)
    assert result.category is ErrorCategory.CONFIGURATION_ERROR


def test_provision_rejects_missing_investigation(migrated_url: str) -> None:
    async def _run() -> AdmissionResult:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            return await _create(_admission(), pool, alloc_id, investigation_id=uuid4())

    result = asyncio.run(_run())
    assert isinstance(result, AdmissionFailure)
    assert result.category is ErrorCategory.CONFIGURATION_ERROR


def test_provision_rejects_cross_project_investigation(migrated_url: str) -> None:
    async def _run() -> AdmissionResult:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                inv_id = await _seed_investigation(conn, project="other")
            return await _create(_admission(), pool, alloc_id, investigation_id=inv_id)

    result = asyncio.run(_run())
    assert isinstance(result, AdmissionFailure)
    assert result.category is ErrorCategory.CONFIGURATION_ERROR


def test_define_then_provision_cannot_change_binding(migrated_url: str) -> None:
    async def _run() -> tuple[AdmissionResult, UUID | None, UUID]:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                bound = await _seed_investigation(conn)
                other = await _seed_investigation(conn)
            defined = await _create(
                _admission(), pool, alloc_id, mode="define", investigation_id=bound
            )
            assert isinstance(defined, DefinedSystemAdmitted)
            changed = await _create(
                _admission(), pool, alloc_id, mode="provision", investigation_id=other
            )
            return changed, await _row_investigation_id(pool, alloc_id), bound

    result, stored, bound = asyncio.run(_run())
    assert isinstance(result, AdmissionFailure)
    assert result.category is ErrorCategory.CONFIGURATION_ERROR
    # The write-once binding recorded at define is untouched by the rejected provision.
    assert stored == bound


def test_define_then_provision_matching_binding_is_allowed(migrated_url: str) -> None:
    async def _run() -> AdmissionResult:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                bound = await _seed_investigation(conn)
            defined = await _create(
                _admission(), pool, alloc_id, mode="define", investigation_id=bound
            )
            assert isinstance(defined, DefinedSystemAdmitted)
            # Re-supplying the same binding on the define-lane is the idempotent re-define.
            return await _create(
                _admission(), pool, alloc_id, mode="define", investigation_id=bound
            )

    result = asyncio.run(_run())
    assert isinstance(result, DefinedSystemAdmitted)
