"""Tests for the post-lock etag repair (ADR-0519, #1725).

``reconcile_row_etag`` runs when a handler's PUT landed outside the advisory lock and it then
found a peer attempt's row for the same key. The property that matters is that it writes an
**observed** etag: an attempt whose PUT landed first can still be the last to take the lock, so a
repair that wrote its own etag would replace a correct row value with a stale one — introducing
the drift it exists to remove.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.catalog import etag_repair
from kdive.artifacts.catalog.etag_repair import reconcile_row_etag
from kdive.artifacts.storage import HeadResult
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import ObjectStore
from tests.clock import STORE_MTIME

_KEY = "local/systems/fixture/console-part-0000-0001.gz"


class _StatStore:
    """Answers ``head`` with ``etag``; ``None`` means the object is gone, ``fault`` raises."""

    def __init__(self, etag: str | None, *, fault: bool = False) -> None:
        self._etag = etag
        self._fault = fault
        self.heads: list[str] = []

    def head(self, key: str) -> HeadResult | None:
        self.heads.append(key)
        if self._fault:
            raise CategorizedError(
                "head_object failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        if self._etag is None:
            return None
        return HeadResult(
            size_bytes=1,
            checksum_sha256=None,
            etag=self._etag,
            last_modified=STORE_MTIME,
            version_id="test-version",
        )


async def _seed_row(conn: psycopg.AsyncConnection, etag: str) -> UUID:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('systems', %s, %s, %s, 'redacted', 'console') RETURNING id",
            (uuid4(), _KEY, etag),
        )
        row = await cur.fetchone()
    assert row is not None
    return cast(UUID, row[0])


async def _row_etag(conn: psycopg.AsyncConnection, row_id: UUID) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT etag FROM artifacts WHERE id = %s", (row_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


def _repair(migrated_url: str, store: _StatStore, row_etag: str) -> str:
    """Seed a row carrying ``row_etag``, repair it against ``store``, return its etag after."""

    async def _go() -> str:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                await conn.set_autocommit(True)
                row_id = await _seed_row(conn, row_etag)
                await reconcile_row_etag(
                    conn,
                    cast(ObjectStore, store),
                    row_id=row_id,
                    object_key=_KEY,
                    row_etag=row_etag,
                )
                return await _row_etag(conn, row_id)

    return asyncio.run(_go())


def test_the_row_is_repointed_at_the_observed_etag(migrated_url: str) -> None:
    """The repair writes what the object holds, not what any particular attempt wrote."""
    assert _repair(migrated_url, _StatStore("etag-observed"), "etag-stale") == "etag-observed"


def test_a_row_that_already_agrees_is_left_alone(migrated_url: str) -> None:
    store = _StatStore("etag-same")
    assert _repair(migrated_url, store, "etag-same") == "etag-same"
    assert store.heads == [_KEY]  # it did stat, and then declined to write


def test_a_vanished_object_leaves_the_row_untouched(migrated_url: str) -> None:
    """No etag describes a missing object better than the one already recorded."""
    assert _repair(migrated_url, _StatStore(None), "etag-stale") == "etag-stale"


def test_a_failed_stat_leaves_the_row_untouched_and_never_raises(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The caller is returning a real result; a metadata repair must not displace it."""
    store = _StatStore(None, fault=True)
    with caplog.at_level(logging.WARNING, logger="kdive.artifacts.catalog.etag_repair"):
        assert _repair(migrated_url, store, "etag-stale") == "etag-stale"
    assert store.heads == [_KEY]
    assert any(_KEY in record.getMessage() for record in caplog.records)
    assert any("CategorizedError" in record.getMessage() for record in caplog.records)
    assert all("head_object failed" not in record.getMessage() for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_a_failed_row_update_leaves_the_row_untouched_and_never_raises(
    migrated_url: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(etag_repair, "_REFRESH_ETAG_SQL", "UPDATE no_such_table SET etag = %s")
    with caplog.at_level(logging.WARNING, logger="kdive.artifacts.catalog.etag_repair"):
        assert _repair(migrated_url, _StatStore("etag-observed"), "etag-stale") == "etag-stale"
    assert any(_KEY in record.getMessage() for record in caplog.records)
    assert any("ProgrammingError" in record.getMessage() for record in caplog.records)
    assert all("no_such_table" not in record.getMessage() for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)
