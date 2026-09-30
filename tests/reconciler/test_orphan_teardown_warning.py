"""A failed orphan teardown is left alone and reported once per failure (#2978, ADR-0435)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import AllocationState, SystemState
from kdive.reconciler.loop import reconcile_once
from kdive.reconciler.repairs import systems as system_repairs
from kdive.reconciler.repairs.systems import (
    repair_orphaned_systems,
    report_stranded_orphan_teardowns,
)
from tests.reconcile_helpers import make_reconcile_config
from tests.reconciler.conftest import FakeReaper, connect, run_repair, seed_system

_KIND = "stranded_orphan_teardowns"
_ROW = "SELECT state, attempt, updated_at FROM jobs WHERE dedup_key = %s"


@pytest.fixture(autouse=True)
def _fresh_dedupe() -> Iterator[None]:
    system_repairs._warned_stranded_teardowns.clear()
    yield
    system_repairs._warned_stranded_teardowns.clear()


async def _orphan_with_failed_teardown(url: str, pool: AsyncConnectionPool) -> UUID:
    """An orphaned `ready` System whose lane-enqueued teardown then dead-lettered."""
    async with await connect(url) as seed:
        system_id = await seed_system(
            seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
        )
    assert await run_repair(pool, repair_orphaned_systems) == 1
    async with await connect(url) as conn:
        await conn.execute(
            "UPDATE jobs SET state = 'failed', attempt = max_attempts, error_category = 'conflict'"
            " WHERE dedup_key = %s",
            (f"{system_id}:teardown",),
        )
    return system_id


async def _row(url: str, system_id: UUID) -> tuple[object, ...] | None:
    async with await connect(url) as conn:
        return await (await conn.execute(_ROW, (f"{system_id}:teardown",))).fetchone()


def _warnings(caplog: pytest.LogCaptureFixture, system_id: UUID) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and str(system_id) in record.getMessage()
    ]


def test_orphan_lane_leaves_failed_teardown_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            system_id = await _orphan_with_failed_teardown(migrated_url, pool)
            before = await _row(migrated_url, system_id)
            count = await run_repair(pool, repair_orphaned_systems)
        assert count == 0
        assert before is not None and before[0] == "failed"
        assert await _row(migrated_url, system_id) == before

    asyncio.run(_run())


def test_stranded_teardown_warns_once_per_failure(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            system_id = await _orphan_with_failed_teardown(migrated_url, pool)
            counts = []
            warned = []
            for bump in (False, False, True):
                if bump:  # the operator's re-run failed again: a new failure
                    async with await connect(migrated_url) as conn:
                        await conn.execute(
                            "UPDATE jobs SET updated_at = updated_at + interval '1 second' "
                            "WHERE dedup_key = %s",
                            (f"{system_id}:teardown",),
                        )
                caplog.clear()
                report = await reconcile_once(pool, FakeReaper(), config=make_reconcile_config())
                counts.append(report.repair_counts[_KIND])
                warned.append(_warnings(caplog, system_id))
            row = await _row(migrated_url, system_id)
        assert counts == [1, 0, 1]
        assert [len(lines) for lines in warned] == [1, 0, 1]
        assert "systems.teardown" in warned[0][0]
        assert row is not None and row[0] == "failed"

    asyncio.run(_run())


def test_stranded_teardown_names_authority_remedy(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            system_id = await _orphan_with_failed_teardown(migrated_url, pool)
            async with await connect(migrated_url) as conn:
                await conn.execute(
                    "UPDATE jobs SET payload = payload || '{\"authority_system_v1\": {}}'::jsonb "
                    "WHERE dedup_key = %s",
                    (f"{system_id}:teardown",),
                )
            count = await run_repair(pool, report_stranded_orphan_teardowns)
        (line,) = _warnings(caplog, system_id)
        assert count == 1
        assert "systems.get" in line and "systems.teardown" not in line

    asyncio.run(_run())


def test_stranded_teardown_skips_tearing_down_system(migrated_url: str) -> None:
    """`repair_stalled_tearing_down_systems` recycles that row, so no warning is owed."""

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            system_id = await _orphan_with_failed_teardown(migrated_url, pool)
            async with await connect(migrated_url) as conn:
                await conn.execute(
                    "UPDATE systems SET state = %s WHERE id = %s",
                    (SystemState.TEARING_DOWN.value, system_id),
                )
            count = await run_repair(pool, report_stranded_orphan_teardowns)
        assert count == 0

    asyncio.run(_run())
