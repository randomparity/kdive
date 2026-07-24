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

from kdive.db.locks import LockScope, _lock_key
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


def test_bind_serializes_with_concurrent_close(migrated_url: str) -> None:
    # The bind's investigation-state read runs under the INVESTIGATION lock close_investigation
    # holds, so a bind racing a close cannot leave a System bound to a just-closed investigation:
    # the bind blocks on the lock, then — the close having won — is rejected as terminal.
    async def _run() -> AdmissionResult:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                inv_id = await _seed_investigation(conn)  # OPEN
            # A separate connection simulates an in-progress close: hold the INVESTIGATION xact
            # lock and mark the investigation closed, uncommitted (the lock releases at commit).
            holder = await AsyncConnection.connect(migrated_url, autocommit=False)
            try:
                await holder.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_lock_key(LockScope.INVESTIGATION, inv_id),),
                )
                await holder.execute(
                    "UPDATE investigations SET state = 'closed' WHERE id = %s", (inv_id,)
                )
                bind = asyncio.create_task(
                    _create(_admission(), pool, alloc_id, investigation_id=inv_id)
                )
                await asyncio.sleep(0.3)
                assert not bind.done()  # blocked on the INVESTIGATION lock, not racing past it
                await holder.commit()  # release the lock; the close is now durable
            finally:
                await holder.close()
            return await bind

    result = asyncio.run(_run())
    assert isinstance(result, AdmissionFailure)  # bind rejected: investigation closed under lock
    assert result.category is ErrorCategory.CONFIGURATION_ERROR
