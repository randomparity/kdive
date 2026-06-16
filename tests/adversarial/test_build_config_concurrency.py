"""Adversarial: ``buildconfig.set`` serialization keeps the row and object in agreement.

The object PUT and the catalog-row upsert are separate writes against non-transactional
stores; ADR-0119 serializes them per fragment name on ``LockScope.BUILD_CONFIG`` (shared with
the seed). These tests attack that lock two ways: a deterministic DB-level contention probe on
the exact lock key, and a genuinely-concurrent two-writer race that must converge to a row
whose sha256 matches the bytes at the reserved key. A single Python process cannot reproduce
true cross-process contention; the probe asserts the lock key + blocking semantics the two
server processes rely on.
"""

from __future__ import annotations

import asyncio
import hashlib

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, _lock_key, advisory_xact_lock
from kdive.mcp.tools.catalog.build_configs import set_build_config
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole
from kdive.store.objectstore import ObjectStore
from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401
from tests.store.conftest import minio_store  # noqa: F401

__all__ = ["migrated_url", "pg_conn", "postgres_url", "minio_store"]

_ADMIN = RequestContext(
    principal="op-1",
    agent_session="sess-1",
    projects=(),
    roles={},
    platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
)


def test_build_config_lock_blocks_a_second_holder(migrated_url: str) -> None:
    """A second connection cannot acquire the BUILD_CONFIG lock for a name while A holds it."""
    key = _lock_key(LockScope.BUILD_CONFIG, "kdump")

    async def _run() -> None:
        conn_a = await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)
        conn_b = await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)
        try:
            async with (
                conn_a.transaction(),
                advisory_xact_lock(conn_a, LockScope.BUILD_CONFIG, "kdump"),
            ):
                held = await conn_b.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,))
                row = await held.fetchone()
                assert row is not None and row[0] is False  # B is blocked while A holds it
            # A's transaction ended; B can now take it.
            got = await conn_b.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,))
            row = await got.fetchone()
            assert row is not None and row[0] is True
        finally:
            await conn_a.close()
            await conn_b.close()

    asyncio.run(_run())


def test_concurrent_set_converges_row_and_object(
    migrated_url: str, minio_store: ObjectStore
) -> None:
    """Two concurrent set calls for one name leave the row sha256 matching the object bytes."""

    async def _run() -> None:
        pool = AsyncConnectionPool(migrated_url, min_size=2, max_size=4, open=False)
        await pool.open()
        try:
            await asyncio.gather(
                set_build_config(
                    pool,
                    lambda: minio_store,
                    _ADMIN,
                    name="kdump",
                    content="AAAA\n",
                    description="a",
                ),
                set_build_config(
                    pool,
                    lambda: minio_store,
                    _ADMIN,
                    name="kdump",
                    content="BBBB\n",
                    description="b",
                ),
            )
            from kdive.mcp.tools.catalog.build_configs import read_build_config

            async with pool.connection() as conn:
                got = await read_build_config(conn, minio_store, name="kdump")
        finally:
            await pool.close()
        # The surviving row's sha256 matches the bytes actually served at the key — no torn pair.
        content = str(got.data["content"])
        assert got.data["sha256"] == hashlib.sha256(content.encode()).hexdigest()
        assert content in ("AAAA\n", "BBBB\n")

    asyncio.run(_run())
