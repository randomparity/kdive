"""Allocation release remains authorized until external-boot cleanup finishes (ADR-0596)."""

from __future__ import annotations

import asyncio

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import ErrorCategory
from kdive.reconciler.repairs.allocations import sweep_expired_allocations
from kdive.security import audit
from kdive.services.allocation.release import reclaim_under_lock, release_with_backstops
from tests.services.external_boot.conftest import SeedActivation


async def _noop_audit(_conn: psycopg.AsyncConnection, _event: audit.AuditEvent) -> None:
    return None


def test_release_refuses_an_uncleaned_activation(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def body() -> None:
        async with (
            await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn,
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
        ):
            seeded = await seeded_activation(
                conn,
                state=ExternalBootActivationState.ABANDONED,
                ready_reservation=True,
            )
            row = await conn.execute(
                "SELECT allocation_id FROM systems WHERE id = %s", (seeded.system_id,)
            )
            allocation_row = await row.fetchone()
            assert allocation_row is not None
            allocation_id = allocation_row[0]

            outcome = await release_with_backstops(
                pool, allocation_id, project="proj", audit_writer=_noop_audit
            )

            assert outcome.released is False
            assert outcome.category is ErrorCategory.CONFLICT
            state = await conn.execute(
                "SELECT state FROM allocations WHERE id = %s", (allocation_id,)
            )
            state_row = await state.fetchone()
            assert state_row is not None
            assert state_row[0] == "granted"

    asyncio.run(body())


def test_reconciler_reclaim_retains_an_uncleaned_activation(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            seeded = await seeded_activation(
                conn,
                state=ExternalBootActivationState.ABANDONED,
                ready_reservation=True,
            )
            cursor = await conn.execute(
                "SELECT allocation_id FROM systems WHERE id = %s", (seeded.system_id,)
            )
            row = await cursor.fetchone()
            assert row is not None
            outcome = await reclaim_under_lock(conn, _noop_audit, row[0], project="proj")
            assert outcome.released is False
            assert outcome.category is ErrorCategory.CONFLICT

    asyncio.run(body())


def test_expiry_retains_an_allocation_needed_for_external_boot_cleanup(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            seeded = await seeded_activation(
                conn,
                state=ExternalBootActivationState.ABANDONED,
                ready_reservation=True,
            )
            cursor = await conn.execute(
                "SELECT allocation_id FROM systems WHERE id = %s", (seeded.system_id,)
            )
            row = await cursor.fetchone()
            assert row is not None
            allocation_id = row[0]
            await conn.execute(
                "UPDATE allocations SET state = 'active', lease_expiry = now() - interval '1s' "
                "WHERE id = %s",
                (allocation_id,),
            )

            assert await sweep_expired_allocations(conn) == 0
            state = await conn.execute(
                "SELECT state FROM allocations WHERE id = %s", (allocation_id,)
            )
            assert await state.fetchone() == ("active",)

    asyncio.run(body())
