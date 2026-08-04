"""Database-scope assertions for concurrency-test wait probes."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, advisory_xact_lock
from tests.db_waits import wait_until_any_backend_waiting, wait_until_backend_waiting


def test_any_backend_wait_ignores_waiter_in_other_database(
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
                        match="no backend began waiting on the expected database lock",
                    ):
                        await wait_until_any_backend_waiting(
                            observer, locktype="advisory", timeout_s=0.1
                        )
                    assert not waiter.done()
            finally:
                if waiter is not None:
                    await asyncio.wait_for(waiter, timeout=5)

    asyncio.run(_run())


def test_any_backend_wait_detects_waiter_in_observer_database(migrated_url: str) -> None:
    async def _run() -> None:
        key = uuid4()
        async with (
            await psycopg.AsyncConnection.connect(migrated_url) as observer,
            await psycopg.AsyncConnection.connect(migrated_url) as holder,
            await psycopg.AsyncConnection.connect(migrated_url) as waiter_conn,
        ):

            async def wait_in_observer_database() -> None:
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
                    waiter = asyncio.create_task(wait_in_observer_database())
                    await wait_until_any_backend_waiting(observer, locktype="advisory")
                    assert not waiter.done()
            finally:
                if waiter is not None:
                    await asyncio.wait_for(waiter, timeout=5)

    asyncio.run(_run())
