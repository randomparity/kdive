"""Database-scope assertions for concurrency-test wait probes."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, wait
from threading import Event
from uuid import uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, advisory_xact_lock
from tests.db_waits import (
    wait_until_any_backend_waiting,
    wait_until_backend_waiting,
    wait_until_blocked_by,
)


def test_any_backend_wait_ignores_waiter_blocked_by_other_backend(
    postgres_url: str, migrated_url: str
) -> None:
    async def _run() -> None:
        key = uuid4()
        async with (
            await psycopg.AsyncConnection.connect(migrated_url) as observer,
            await psycopg.AsyncConnection.connect(postgres_url) as other_holder,
            await psycopg.AsyncConnection.connect(postgres_url) as other_waiter,
        ):
            assert observer.info.dbname != other_holder.info.dbname

            async def wait_in_other_database() -> None:
                async with (
                    other_waiter.transaction(),
                    advisory_xact_lock(other_waiter, LockScope.INVESTIGATION, key),
                ):
                    pass

            waiter: asyncio.Task[None] | None = None
            try:
                async with (
                    other_holder.transaction(),
                    advisory_xact_lock(other_holder, LockScope.INVESTIGATION, key),
                ):
                    waiter = asyncio.create_task(wait_in_other_database())
                    await wait_until_backend_waiting(
                        observer, other_waiter.info.backend_pid, locktype="advisory"
                    )
                    with pytest.raises(
                        AssertionError,
                        match="no backend began waiting on the expected lock held by the observer",
                    ):
                        await wait_until_any_backend_waiting(
                            observer, locktype="advisory", timeout_s=0.1
                        )
                    assert not waiter.done()
            finally:
                if waiter is not None:
                    await asyncio.wait_for(waiter, timeout=5)

    asyncio.run(_run())


def test_blocked_wait_returns_once_the_waiter_blocks_on_the_named_blocker(
    postgres_url: str,
) -> None:
    key = uuid4().int % (2**63 - 1)
    with (
        psycopg.connect(postgres_url, autocommit=True) as observer,
        psycopg.connect(postgres_url) as blocker,
        psycopg.connect(postgres_url, autocommit=True) as waiter,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        blocker.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        future = executor.submit(waiter.execute, "SELECT pg_advisory_xact_lock(%s)", (key,))
        try:
            wait_until_blocked_by(
                observer,
                waiter_pid=waiter.info.backend_pid,
                blocker_pid=blocker.info.backend_pid,
                future=future,
                expectation="waiter did not block on the advisory lock",
            )
            assert not future.done()
        finally:
            blocker.commit()
        future.result(timeout=5)


def test_blocked_wait_surfaces_the_exception_of_a_future_that_raised(postgres_url: str) -> None:
    """A call that died instead of blocking reports its own failure, not "did not block"."""

    def die() -> None:
        raise RuntimeError("worker connection refused before the lock was ever requested")

    with (
        psycopg.connect(postgres_url, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(die)
        wait([future], timeout=5)
        with pytest.raises(AssertionError) as raised:
            wait_until_blocked_by(
                observer,
                waiter_pid=observer.info.backend_pid,
                blocker_pid=observer.info.backend_pid,
                future=future,
                expectation="heartbeat did not block on its exact running job row",
            )

    assert "worker connection refused before the lock was ever requested" in str(raised.value)
    cause = raised.value.__cause__
    assert isinstance(cause, RuntimeError)
    assert str(cause) == "worker connection refused before the lock was ever requested"


def test_blocked_wait_surfaces_the_result_of_a_future_that_never_blocked(
    postgres_url: str,
) -> None:
    with (
        psycopg.connect(postgres_url, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(lambda: "renewed-without-any-contention")
        wait([future], timeout=5)
        with pytest.raises(AssertionError) as raised:
            wait_until_blocked_by(
                observer,
                waiter_pid=observer.info.backend_pid,
                blocker_pid=observer.info.backend_pid,
                future=future,
                expectation="heartbeat did not block on its exact running job row",
            )

    assert "renewed-without-any-contention" in str(raised.value)
    assert raised.value.__cause__ is None


def test_blocked_wait_timeout_names_the_waiter_pid_and_its_lock_state(postgres_url: str) -> None:
    key = uuid4().int % (2**63 - 1)
    release = Event()
    with (
        psycopg.connect(postgres_url, autocommit=True) as observer,
        psycopg.connect(postgres_url) as idle_waiter,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        idle_waiter.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        waiter_pid = idle_waiter.info.backend_pid
        future = executor.submit(release.wait, 30)
        try:
            with pytest.raises(AssertionError) as raised:
                wait_until_blocked_by(
                    observer,
                    waiter_pid=waiter_pid,
                    blocker_pid=observer.info.backend_pid,
                    future=future,
                    expectation="acquisition did not block on the reclaiming generation row",
                    timeout_s=0.1,
                )
        finally:
            release.set()
            future.result(timeout=5)
        idle_waiter.rollback()

    message = str(raised.value)
    assert "acquisition did not block on the reclaiming generation row" in message
    assert f"waiter pid {waiter_pid}" in message
    assert "idle in transaction" in message
    assert "advisory" in message


def test_any_backend_wait_detects_waiter_blocked_by_observer(migrated_url: str) -> None:
    async def _run() -> None:
        key = uuid4()
        async with (
            await psycopg.AsyncConnection.connect(migrated_url) as holder,
            await psycopg.AsyncConnection.connect(migrated_url) as waiter_conn,
        ):

            async def wait_on_observer() -> None:
                async with (
                    waiter_conn.transaction(),
                    advisory_xact_lock(waiter_conn, LockScope.INVESTIGATION, key),
                ):
                    pass

            waiter: asyncio.Task[None] | None = None
            try:
                async with (
                    holder.transaction(),
                    advisory_xact_lock(holder, LockScope.INVESTIGATION, key),
                ):
                    waiter = asyncio.create_task(wait_on_observer())
                    await wait_until_any_backend_waiting(holder, locktype="advisory")
                    assert not waiter.done()
            finally:
                if waiter is not None:
                    await asyncio.wait_for(waiter, timeout=5)

    asyncio.run(_run())
