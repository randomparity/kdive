from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

from psycopg import AsyncConnection

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

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        if self._exc is not None:
            raise self._exc
        return FetchedArtifact(self._data, Sensitivity.SENSITIVE, "build")


def _conn(row: dict[str, Any] | None) -> AsyncConnection[Any]:
    return cast(AsyncConnection[Any], _FakeConn(row))


def test_no_row_returns_none():
    got = asyncio.run(load_effective_config(_conn(None), uuid4(), store_factory=lambda: _Store()))
    assert got is None


def test_present_config_parses():
    conn = _conn({"object_key": "local/runs/x/effective_config"})
    got = asyncio.run(load_effective_config(conn, uuid4(), store_factory=lambda: _Store(_GOOD)))
    assert got is not None and got.is_enabled("KEXEC")


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
