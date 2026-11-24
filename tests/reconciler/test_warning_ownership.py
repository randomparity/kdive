"""Teardown warning observations belong to each reconciler, not its pool or process."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import datetime
from functools import partial
from typing import Literal
from uuid import UUID

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db import migrate
from kdive.domain.capacity.state import AllocationState, JobState, SystemState
from kdive.reconciler.loop import Reconciler
from kdive.reconciler.repairs import systems as system_repairs

# Keep peer-database cleanup with the existing fixture owner.
from tests.db.conftest import (
    _cluster_global_role_lock,  # noqa: PLC2701
    _drop_worker_db,  # noqa: PLC2701
    _provision_worker_db,  # noqa: PLC2701
    _server_url_without_db,  # noqa: PLC2701
)
from tests.reconcile_helpers import make_reconcile_config
from tests.reconciler.conftest import FakeReaper, connect, run_repair, seed_system
from tests.reconciler.recovery_support import seed_teardown_job as _seed_teardown_job

type WarningFamily = Literal["stranded", "stalled"]


@pytest.fixture
def peer_warning_database(postgres_url: str) -> Iterator[str]:
    server = _server_url_without_db(postgres_url)
    peer_url, name = _provision_worker_db(server)
    try:
        with (
            _cluster_global_role_lock(postgres_url),
            psycopg.connect(peer_url, autocommit=True) as conn,
        ):
            migrate.apply_migrations(conn)
        yield peer_url
    finally:
        _drop_worker_db(server, name)


async def _seed_warning(
    url: str,
    family: WarningFamily,
    *,
    system_id: UUID | None = None,
) -> UUID:
    async with await connect(url) as conn:
        seeded = await seed_system(
            conn,
            system_state=SystemState.READY if family == "stranded" else SystemState.TEARING_DOWN,
            alloc_state=AllocationState.RELEASED
            if family == "stranded"
            else AllocationState.ACTIVE,
        )
        if system_id is not None:
            await conn.execute("UPDATE systems SET id=%s WHERE id=%s", (system_id, seeded))
            seeded = system_id
        await _seed_teardown_job(conn, seeded, state=JobState.FAILED)
        marker = {"authority_system_v1": {"marker": 1}} if family == "stalled" else {}
        await conn.execute(
            "UPDATE jobs SET error_category='conflict', payload=payload || %s WHERE dedup_key=%s",
            (Jsonb(marker), f"{seeded}:teardown"),
        )
        return seeded


async def _warnings_in_pass(
    runner: Reconciler,
    family: WarningFamily,
    system_id: UUID,
    caplog: pytest.LogCaptureFixture,
) -> int:
    caplog.clear()
    report = await runner.run_once()
    count = sum(
        record.levelno == logging.WARNING and str(system_id) in record.getMessage()
        for record in caplog.records
    )
    assert report.repair_counts["stranded_orphan_teardowns"] == (
        count if family == "stranded" else 0
    )
    assert report.repair_counts["stalled_tearing_down_systems"] == 0
    return count


@pytest.mark.parametrize("family", ["stranded", "stalled"])
def test_reconcilers_sharing_config_and_pool_have_independent_warning_history(
    migrated_url: str, family: WarningFamily, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)

    async def run() -> None:
        system_id = await _seed_warning(migrated_url, family)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            config = make_reconcile_config()
            first = Reconciler(pool, FakeReaper(), config=config)
            peer = Reconciler(pool, FakeReaper(), config=config)
            counts = [
                await _warnings_in_pass(owner, family, system_id, caplog)
                for owner in (first, peer, first, peer)
            ]
        assert counts == [1, 1, 0, 0], "another runner inherited warning suppression"

    asyncio.run(run())


@pytest.mark.parametrize("family", ["stranded", "stalled"])
def test_an_empty_peer_pass_cannot_clear_another_runners_history(
    migrated_url: str,
    peer_warning_database: str,
    family: WarningFamily,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)

    async def run() -> None:
        system_id = await _seed_warning(migrated_url, family)
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
            AsyncConnectionPool(peer_warning_database, min_size=1, max_size=4) as peer_pool,
        ):
            config = make_reconcile_config()
            first = Reconciler(pool, FakeReaper(), config=config)
            peer = Reconciler(peer_pool, FakeReaper(), config=config)
            counts = [
                await _warnings_in_pass(owner, family, system_id, caplog)
                for owner in (first, peer, first)
            ]
        assert counts == [1, 0, 0], "an empty peer pass cleared another runner's history"

    asyncio.run(run())


@pytest.mark.parametrize("family", ["stranded", "stalled"])
def test_equal_system_ids_in_separate_databases_do_not_share_warning_history(
    migrated_url: str,
    peer_warning_database: str,
    family: WarningFamily,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)

    async def run() -> None:
        system_id = await _seed_warning(migrated_url, family)
        assert await _seed_warning(peer_warning_database, family, system_id=system_id) == system_id
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
            AsyncConnectionPool(peer_warning_database, min_size=1, max_size=4) as peer_pool,
        ):
            config = make_reconcile_config()
            first = Reconciler(pool, FakeReaper(), config=config)
            peer = Reconciler(peer_pool, FakeReaper(), config=config)
            counts = [
                await _warnings_in_pass(owner, family, system_id, caplog)
                for owner in (first, peer, first, peer)
            ]
        assert counts == [1, 1, 0, 0], "a matching UUID in another database shared warning history"

    asyncio.run(run())


@pytest.mark.parametrize("family", ["stranded", "stalled"])
def test_standalone_history_is_explicit_and_pruned_only_after_a_successful_read(
    migrated_url: str, family: WarningFamily, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)

    async def run() -> None:
        system_id = await _seed_warning(migrated_url, family)
        history = system_repairs.TeardownWarningHistory()
        repair = (
            system_repairs.report_stranded_orphan_teardowns
            if family == "stranded"
            else system_repairs.repair_stalled_tearing_down_systems
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
            await connect(migrated_url) as conn,
        ):
            before = await (
                await conn.execute(
                    "SELECT state, attempt, updated_at, payload FROM jobs WHERE dedup_key=%s",
                    (f"{system_id}:teardown",),
                )
            ).fetchone()
            assert before is not None
            counts = []
            for supplied in (history, history, None, None):
                caplog.clear()
                if supplied is None:
                    await run_repair(pool, repair)
                else:
                    await run_repair(pool, partial(repair, warnings=supplied))
                counts.append(sum(str(system_id) in r.getMessage() for r in caplog.records))
            assert counts == [1, 0, 1, 1]
            await conn.execute("UPDATE systems SET state='torn_down' WHERE id=%s", (system_id,))
            assert await run_repair(pool, lambda conn: repair(conn, warnings=history)) == 0
            assert not (history.stranded if family == "stranded" else history.stalled)
            await conn.execute(
                "UPDATE systems SET state=%s WHERE id=%s",
                ("ready" if family == "stranded" else "tearing_down", system_id),
            )
            caplog.clear()
            await run_repair(pool, lambda conn: repair(conn, warnings=history))
            assert sum(str(system_id) in r.getMessage() for r in caplog.records) == 1
            after = await (
                await conn.execute(
                    "SELECT state, attempt, updated_at, payload FROM jobs WHERE dedup_key=%s",
                    (f"{system_id}:teardown",),
                )
            ).fetchone()
            assert after == before

    asyncio.run(run())


@pytest.mark.parametrize("family", ["stranded", "stalled"])
def test_query_failure_retains_both_history_maps(migrated_url: str, family: WarningFamily) -> None:
    async def run() -> None:
        system_id = await _seed_warning(migrated_url, family)
        async with await connect(migrated_url) as reader, await connect(migrated_url) as blocker:
            row = await (
                await reader.execute(
                    "SELECT updated_at FROM jobs WHERE dedup_key=%s", (f"{system_id}:teardown",)
                )
            ).fetchone()
            assert row is not None and isinstance(row[0], datetime)
            history = system_repairs.TeardownWarningHistory(
                stranded={system_id: row[0]}, stalled={system_id: "authority_system_marked"}
            )
            before = (dict(history.stranded), dict(history.stalled))
            repair = (
                system_repairs.report_stranded_orphan_teardowns
                if family == "stranded"
                else system_repairs.repair_stalled_tearing_down_systems
            )
            await reader.execute("SET lock_timeout='50ms'")
            async with blocker.transaction():
                await blocker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await repair(reader, warnings=history)
            assert (history.stranded, history.stalled) == before
            assert reader.info.transaction_status == TransactionStatus.IDLE

    asyncio.run(run())


def test_changed_stalled_cause_warns_again_within_one_runner(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)

    async def run() -> None:
        system_id = await _seed_warning(migrated_url, "stalled")
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            runner = Reconciler(pool, FakeReaper(), config=make_reconcile_config())
            counts = [await _warnings_in_pass(runner, "stalled", system_id, caplog)]
            counts.append(await _warnings_in_pass(runner, "stalled", system_id, caplog))
            async with await connect(migrated_url) as conn:
                await conn.execute(
                    "UPDATE jobs SET payload=(payload - 'authority_system_v1') || %s "
                    "WHERE dedup_key=%s",
                    (Jsonb({"external_boot_authority_v1": {"marker": 1}}), f"{system_id}:teardown"),
                )
            counts.append(await _warnings_in_pass(runner, "stalled", system_id, caplog))
            counts.append(await _warnings_in_pass(runner, "stalled", system_id, caplog))
        assert counts == [1, 0, 1, 0]

    asyncio.run(run())
