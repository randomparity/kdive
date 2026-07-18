"""Tests for Resource discovery registration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.discovery import ResourceRecord
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.core.resource_registration import (
    register_discovered_resource,
    register_or_refresh_discovered_resource,
)


class _Discovery:
    def __init__(self, cap: int = 2, extra: dict[str, object] | None = None) -> None:
        self.cap = cap
        self.extra = extra or {}
        self.calls = 0
        self.fail = False

    def list_resources(self) -> list[ResourceRecord]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("libvirt unreachable")
        return [
            ResourceRecord(
                resource_id="qemu:///system",
                kind=ResourceKind.LOCAL_LIBVIRT,
                capabilities={
                    "arch": "x86_64",
                    "vcpus": 8,
                    "memory_mb": 16384,
                    "transports": ["gdbstub"],
                    CONCURRENT_ALLOCATION_CAP_KEY: self.cap,
                    **self.extra,
                },
                status=ResourceStatus.AVAILABLE,
            )
        ]


@asynccontextmanager
async def _pg(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


def test_register_discovered_resource_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pg(migrated_url) as conn:
            first = await register_discovered_resource(
                conn,
                _Discovery(cap=2).list_resources()[0],
                pool="local-libvirt",
                cost_class="local",
            )
            second = await register_discovered_resource(
                conn,
                _Discovery(cap=5).list_resources()[0],
                pool="local-libvirt",
                cost_class="local",
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM resources")
                row = await cur.fetchone()
        assert first.host_uri == "qemu:///system"
        assert first.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 2
        assert second.id == first.id
        assert second.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 5
        assert row is not None and row[0] == 1

    asyncio.run(_run())


def test_ensure_discovered_resource_registered_bootstraps_one_row(migrated_url: str) -> None:
    async def _run() -> None:
        discovery = _Discovery(cap=2)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, discovery)
            await _ensure(pool, discovery)
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute("SELECT kind, host_uri FROM resources")
            rows = await cur.fetchall()
        assert rows == [("local-libvirt", "qemu:///system")]
        assert discovery.calls == 2  # insert reads once; the second pass refreshes (reads again)

    asyncio.run(_run())


def test_refresh_gains_missing_capability_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery())  # inserts without pseries_fadump
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT id, managed_by FROM resources")
                before = await cur.fetchone()
            await _ensure(pool, _Discovery(extra={"pseries_fadump": True}))
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, managed_by, capabilities->>'pseries_fadump' FROM resources"
                )
                after = await cur.fetchone()
        assert before is not None and after is not None
        assert after[0] == before[0]  # id unchanged
        assert after[1] == before[1]  # managed_by unchanged
        assert after[2] == "true"  # gained the key

    asyncio.run(_run())


def test_refresh_updates_changed_discovery_value(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery(extra={"guest_arches": ["x86_64"]}))
            await _ensure(pool, _Discovery(extra={"guest_arches": ["x86_64", "ppc64le"]}))
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute("SELECT capabilities->'guest_arches' FROM resources")
            row = await cur.fetchone()
        assert row is not None and row[0] == ["x86_64", "ppc64le"]

    asyncio.run(_run())


def test_refresh_preserves_operator_cap_and_gains_new_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery(cap=1))
            # Operator sets the cap directly on the discovery row (ops.set_host_capacity shape).
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET capabilities = "
                    "capabilities || jsonb_build_object('concurrent_allocation_cap', 5)"
                )
                await conn.commit()
            # A later deploy carries a net-new key AND the env-default cap (1).
            await _ensure(pool, _Discovery(cap=1, extra={"pseries_fadump": True}))
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT capabilities->>'concurrent_allocation_cap', "
                "capabilities->>'pseries_fadump' FROM resources"
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "5"  # operator cap preserved, NOT reverted to env default 1
        assert row[1] == "true"  # net-new discovery key still gained

    asyncio.run(_run())


def test_refresh_preserves_status_pool_cost_and_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery())
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET status = 'degraded', pool = 'custom', "
                    "cost_class = 'premium', cordoned = true"
                )
                await conn.commit()
            await _ensure(pool, _Discovery(extra={"pseries_fadump": True}))
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT status, pool, cost_class, cordoned, "
                "capabilities->>'pseries_fadump' FROM resources"
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[:4] == ("degraded", "custom", "premium", True)  # all preserved
        assert row[4] == "true"  # capabilities still refreshed

    asyncio.run(_run())


def test_refresh_read_failure_keeps_existing_capabilities(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery(cap=2, extra={"pseries_fadump": True}))
            failing = _Discovery()
            failing.fail = True
            await _ensure(pool, failing)  # must NOT raise
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute("SELECT capabilities->>'pseries_fadump' FROM resources")
            row = await cur.fetchone()
        assert row is not None and row[0] == "true"  # existing capabilities intact

    asyncio.run(_run())


def test_refresh_change_guard_skips_write_when_unchanged(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery())
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT xmin FROM resources")
                before = await cur.fetchone()
            await _ensure(pool, _Discovery())  # identical discovery record
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT xmin FROM resources")
                after = await cur.fetchone()
        assert before is not None and after is not None
        assert after[0] == before[0]  # no row write: xmin unchanged

    asyncio.run(_run())


def test_absent_branch_discovery_failure_raises(migrated_url: str) -> None:
    async def _run() -> None:
        failing = _Discovery()
        failing.fail = True
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            try:
                await _ensure(pool, failing)  # empty DB → absent branch must raise
            except RuntimeError:
                pass
            else:
                raise AssertionError("absent-branch discovery failure did not raise")
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM resources")
                row = await cur.fetchone()
        assert row is not None and row[0] == 0  # nothing inserted

    asyncio.run(_run())


def test_concurrent_operator_cap_is_not_lost_by_refresh(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=4) as pool:
            await _ensure(pool, _Discovery(cap=1))
            async with _pg(migrated_url) as op_conn:
                await op_conn.execute("BEGIN")
                await op_conn.execute(
                    "SELECT id FROM resources WHERE kind = %s AND host_uri = %s FOR UPDATE",
                    ("local-libvirt", "qemu:///system"),
                )
                # Refresh carries a net-new key; it must block on the row lock op_conn holds.
                refresh = asyncio.create_task(
                    _ensure(pool, _Discovery(cap=1, extra={"pseries_fadump": True}))
                )
                await asyncio.sleep(0.3)
                assert not refresh.done()  # blocked on the FOR UPDATE row lock
                await op_conn.execute(
                    "UPDATE resources SET capabilities = "
                    "capabilities || jsonb_build_object('concurrent_allocation_cap', 5)"
                )
                await op_conn.execute("COMMIT")
                await asyncio.wait_for(refresh, timeout=5)
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT capabilities->>'concurrent_allocation_cap', "
                "capabilities->>'pseries_fadump' FROM resources"
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "5"  # operator cap read under the lock and preserved
        assert row[1] == "true"  # refresh still rolled out the net-new key

    asyncio.run(_run())


async def _ensure(pool: AsyncConnectionPool, discovery: _Discovery) -> None:
    await register_or_refresh_discovered_resource(
        pool,
        discovery,
        kind=ResourceKind.LOCAL_LIBVIRT,
        resource_id="qemu:///system",
        pool_name="local-libvirt",
        cost_class="local",
    )
