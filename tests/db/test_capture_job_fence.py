"""One per-job capture ownership fence, shared by the worker and the reaper (ADR-0556/0558)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import AsyncConnection

from kdive.db.locks import try_capture_job_fence

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kdive"
_FENCE_NAMESPACE = "kdive:job:"
_DEFINING_MODULE = _SRC_ROOT / "db" / "locks.py"


def test_the_fence_key_is_spelled_out_in_exactly_one_source_module() -> None:
    """A reaper that hashes a different string fences against nothing at all."""
    modules = sorted(_SRC_ROOT.rglob("*.py"))
    spelling_it_out = [
        module for module in modules if _FENCE_NAMESPACE in module.read_text(encoding="utf-8")
    ]

    assert len(modules) > 100, "the source scan walked an unexpectedly small tree"
    assert spelling_it_out == [_DEFINING_MODULE]


def test_the_fence_is_free_when_no_capture_holds_it(migrated_url: str) -> None:
    async def _run() -> None:
        async with (
            await AsyncConnection.connect(migrated_url) as conn,
            conn.transaction(),
        ):
            assert await try_capture_job_fence(conn, uuid4()) is True

    asyncio.run(_run())


def test_a_worker_session_fence_blocks_the_reaper_on_the_same_job(migrated_url: str) -> None:
    """The worker holds the session form; the reaper's try must contend on the same bigint."""
    job_id = uuid4()
    other_job = uuid4()

    async def _run() -> None:
        async with (
            await AsyncConnection.connect(migrated_url, autocommit=True) as worker,
            await AsyncConnection.connect(migrated_url) as reconciler,
        ):
            # The exact statement kdive.jobs.capture_operations.supervisor holds its fence with.
            await worker.execute(
                "SELECT pg_advisory_lock(hashtextextended('kdive:job:' || %s::text, 1951))",
                (job_id,),
            )
            async with reconciler.transaction():
                assert await try_capture_job_fence(reconciler, job_id) is False
                assert await try_capture_job_fence(reconciler, other_job) is True
            await worker.execute(
                "SELECT pg_advisory_unlock(hashtextextended('kdive:job:' || %s::text, 1951))",
                (job_id,),
            )
            async with reconciler.transaction():
                assert await try_capture_job_fence(reconciler, job_id) is True

    asyncio.run(_run())


def test_the_fence_refuses_to_run_outside_a_transaction(migrated_url: str) -> None:
    """A lock taken with no transaction open has already auto-released (ADR-0005)."""

    async def _run() -> None:
        async with await AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            try:
                await try_capture_job_fence(conn, uuid4())
            except RuntimeError as error:
                assert "transaction" in str(error)
            else:  # pragma: no cover - the guard is the point of the test
                raise AssertionError("the fence accepted an autocommit connection")

    asyncio.run(_run())


def test_the_fence_releases_with_its_transaction(migrated_url: str) -> None:
    job_id = uuid4()

    async def _run() -> None:
        async with (
            await AsyncConnection.connect(migrated_url) as first,
            await AsyncConnection.connect(migrated_url) as second,
        ):
            async with first.transaction():
                assert await try_capture_job_fence(first, job_id) is True
                async with second.transaction():
                    assert await try_capture_job_fence(second, job_id) is False
            async with second.transaction():
                assert await try_capture_job_fence(second, job_id) is True

    asyncio.run(_run())


def test_the_migrated_fixture_is_a_real_database(migrated_url: str) -> None:
    """Keeps the contention tests from passing against a URL nothing is listening on."""
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
