"""Allocation release remains authorized until external-boot cleanup finishes (ADR-0596)."""

from __future__ import annotations

import asyncio
import logging

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import ErrorCategory
from kdive.reconciler.repairs import allocations as allocation_repairs
from kdive.reconciler.repairs.allocations import sweep_expired_allocations
from kdive.security import audit
from kdive.services.allocation.release import reclaim_under_lock, release_with_backstops
from kdive.services.external_boot.admission import DENIAL_REASON
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


def _repair_records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    """The repair module's own records at exactly ``level`` — never a sibling logger's."""
    return [
        record
        for record in caplog.records
        if record.name == allocation_repairs.__name__ and record.levelno == level
    ]


def test_expiry_retains_an_allocation_needed_for_external_boot_cleanup(
    migrated_url: str, seeded_activation: SeedActivation, caplog: pytest.LogCaptureFixture
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

            with caplog.at_level(logging.INFO, logger=allocation_repairs.__name__):
                assert await sweep_expired_allocations(conn) == 0
            state = await conn.execute(
                "SELECT state FROM allocations WHERE id = %s", (allocation_id,)
            )
            assert await state.fetchone() == ("active",)

            # The skip must be attributable, not silent: the sibling no-op returns in
            # `_expire_one` log nothing, so without this the refusal is indistinguishable
            # from a clean pass (#2519). The reason is what separates this record from the
            # sweep's `except Exception` handler, which logs on the same logger.
            denials = _repair_records(caplog, logging.WARNING)
            assert len(denials) == 1
            message = denials[0].getMessage()
            assert str(allocation_id) in message
            assert DENIAL_REASON in message

    asyncio.run(body())


def test_expiry_without_a_restricting_activation_logs_no_denial(
    migrated_url: str, seeded_activation: SeedActivation, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal line is confined to the `except` branch; a clean reclaim keeps its INFO."""

    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            # `cleanup_complete` is what stops `get_restricting_for_system` matching, so the
            # guard admits and the same allocation takes the reclaiming path instead.
            seeded = await seeded_activation(
                conn,
                state=ExternalBootActivationState.ABANDONED,
                ready_reservation=True,
                cleanup_complete=True,
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

            with caplog.at_level(logging.INFO, logger=allocation_repairs.__name__):
                assert await sweep_expired_allocations(conn) == 1
            state = await conn.execute(
                "SELECT state FROM allocations WHERE id = %s", (allocation_id,)
            )
            assert await state.fetchone() == ("expired",)

            assert _repair_records(caplog, logging.WARNING) == []
            reclaims = _repair_records(caplog, logging.INFO)
            assert len(reclaims) == 1
            assert str(allocation_id) in reclaims[0].getMessage()

    asyncio.run(body())
