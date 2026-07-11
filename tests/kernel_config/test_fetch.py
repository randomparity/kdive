from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import FetchedArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.kernel_config.fetch import load_effective_config

_GOOD = b"CONFIG_KEXEC=y\nCONFIG_PROC_VMCORE=y\n"


class _FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: object) -> None:
        self._executed = (sql, params)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _FakeConn:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def cursor(self, *, row_factory: object) -> _FakeCursor:
        return _FakeCursor(self._row)


class _Store:
    def __init__(self, data: bytes = b"", exc: Exception | None = None) -> None:
        self._data, self._exc = data, exc
        self.keys: list[str] = []

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        self.keys.append(key)
        if self._exc is not None:
            raise self._exc
        return FetchedArtifact(self._data, Sensitivity.SENSITIVE, "build")


def _conn(row: dict[str, Any] | None) -> AsyncConnection[Any]:
    return cast(AsyncConnection[Any], _FakeConn(row))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def test_no_row_returns_none():
    got = asyncio.run(load_effective_config(_conn(None), uuid4(), store_factory=lambda: _Store()))
    assert got is None


def test_present_config_parses():
    conn = _conn({"object_key": "local/runs/x/effective_config"})
    got = asyncio.run(load_effective_config(conn, uuid4(), store_factory=lambda: _Store(_GOOD)))
    assert got is not None and got.is_enabled("KEXEC")


def test_present_config_selects_run_owned_effective_config_from_schema(migrated_url: str):
    async def _run() -> None:
        run_id = uuid4()
        other_run_id = uuid4()
        expected_key = f"local/runs/{run_id}/effective_config"
        store = _Store(_GOOD)
        async with (
            _pool(migrated_url) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                "retention_class) VALUES "
                "(%s, %s, %s, %s, %s, %s), "
                "(%s, %s, %s, %s, %s, %s), "
                "(%s, %s, %s, %s, %s, %s), "
                "(%s, %s, %s, %s, %s, %s)",
                (
                    "runs",
                    run_id,
                    f"local/runs/{run_id}/kernel",
                    "etag",
                    "sensitive",
                    "build",
                    "runs",
                    run_id,
                    expected_key,
                    "etag",
                    "sensitive",
                    "build",
                    "runs",
                    other_run_id,
                    f"local/runs/{other_run_id}/effective_config",
                    "etag",
                    "sensitive",
                    "build",
                    "systems",
                    run_id,
                    f"local/systems/{run_id}/effective_config",
                    "etag",
                    "sensitive",
                    "build",
                ),
            )
            got = await load_effective_config(conn, run_id, store_factory=lambda: store)
        assert got is not None and got.is_enabled("PROC_VMCORE")
        assert store.keys == [expected_key]

    asyncio.run(_run())


def test_store_error_fails_open_to_none():
    conn = _conn({"object_key": "k"})
    exc = CategorizedError("gone", category=ErrorCategory.STALE_HANDLE)
    got = asyncio.run(load_effective_config(conn, uuid4(), store_factory=lambda: _Store(exc=exc)))
    assert got is None


def test_degenerate_config_fails_open_to_none():
    conn = _conn({"object_key": "k"})
    got = asyncio.run(
        load_effective_config(conn, uuid4(), store_factory=lambda: _Store(b"# empty\n"))
    )
    assert got is None
