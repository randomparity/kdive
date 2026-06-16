"""Unit tests for the build-config catalog repository (ADR-0096)."""

from __future__ import annotations

import asyncio
import hashlib

import psycopg
import pytest

from kdive.build_configs.catalog import (
    BuildConfigEntry,
    get_build_config,
    get_build_config_sync,
    parse_build_config_row,
    read_build_config_provenance,
    upsert_config_build_config,
    upsert_operator_build_config,
)
from kdive.db import migrate
from kdive.domain.errors import CategorizedError, ErrorCategory

# Re-use the disposable-Postgres fixtures from the db test suite.
from tests.db.conftest import migrated_url, pg_conn, postgres_url

__all__ = ["migrated_url", "pg_conn", "postgres_url"]

_OBJECT_KEY = "system/build-configs/kdump/kdump.config"
_SHA = "abc123"
_DESCRIPTION = "kdump options"


def _insert_kdump_row(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO build_config_catalog (name, object_key, sha256, description) "
        "VALUES (%s, %s, %s, %s)",
        ("kdump", _OBJECT_KEY, _SHA, _DESCRIPTION),
    )


_EXPECTED_ENTRY = BuildConfigEntry(
    name="kdump",
    object_key=_OBJECT_KEY,
    sha256=_SHA,
    description=_DESCRIPTION,
    source="seed",
)


def test_parse_build_config_row_round_trips_fields() -> None:
    entry = parse_build_config_row(
        {
            "name": "kdump",
            "object_key": "system/build-configs/kdump/kdump.config",
            "sha256": "abc",
            "description": "kdump options",
            "source": "seed",
        }
    )
    assert entry == BuildConfigEntry(
        name="kdump",
        object_key="system/build-configs/kdump/kdump.config",
        sha256="abc",
        description="kdump options",
        source="seed",
    )


def test_parse_build_config_row_rejects_missing_required_column() -> None:
    with pytest.raises(CategorizedError) as exc:
        parse_build_config_row(
            {
                "name": "kdump",
                "object_key": "system/build-configs/kdump/kdump.config",
                "sha256": "abc",
            }
        )
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details == {"column": "description"}


def test_parse_build_config_row_rejects_non_string_required_value() -> None:
    with pytest.raises(CategorizedError) as exc:
        parse_build_config_row(
            {
                "name": "kdump",
                "object_key": "system/build-configs/kdump/kdump.config",
                "sha256": "abc",
                "description": None,
            }
        )
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details == {"column": "description"}


def test_verify_sha256_rejects_mismatch() -> None:
    entry = BuildConfigEntry("kdump", "k", sha256="deadbeef", description="", source="seed")
    with pytest.raises(CategorizedError) as exc:
        entry.verify_bytes(b"the wrong bytes")
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_verify_sha256_accepts_match() -> None:
    data = b"CONFIG_CRASH_DUMP=y\n"
    digest = hashlib.sha256(data).hexdigest()
    entry = BuildConfigEntry("kdump", "k", sha256=digest, description="", source="seed")
    entry.verify_bytes(data)  # does not raise


# ---------------------------------------------------------------------------
# DB-backed repository reads (real connection; require Docker, skip otherwise)
# ---------------------------------------------------------------------------


def test_get_build_config_sync_returns_entry(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_kdump_row(pg_conn)
    entry = get_build_config_sync(pg_conn, "kdump")
    assert entry == _EXPECTED_ENTRY


def test_get_build_config_sync_returns_none_for_absent_name(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    assert get_build_config_sync(pg_conn, "nope") is None


def test_get_build_config_returns_entry(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO build_config_catalog (name, object_key, sha256, description) "
                "VALUES (%s, %s, %s, %s)",
                ("kdump", _OBJECT_KEY, _SHA, _DESCRIPTION),
            )
            entry = await get_build_config(conn, "kdump")
        assert entry == _EXPECTED_ENTRY

    asyncio.run(_run())


def test_get_build_config_returns_none_for_absent_name(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            assert await get_build_config(conn, "nope") is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Config-source writer + provenance reader (ADR-0122; require Docker, skip otherwise)
# ---------------------------------------------------------------------------


def test_upsert_config_writes_source_config(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await upsert_config_build_config(
                conn, "kdump", "system/build-configs/kdump/kdump.config", "abc123", "desc"
            )
            prov = await read_build_config_provenance(conn, "kdump")
        assert prov == ("abc123", "config", "desc")

    asyncio.run(_run())


def test_upsert_config_clobbers_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await upsert_operator_build_config(conn, "kdump", "k", "op_sha", "op desc")
            await upsert_config_build_config(conn, "kdump", "k2", "cfg_sha", "cfg desc")
            prov = await read_build_config_provenance(conn, "kdump")
        assert prov == ("cfg_sha", "config", "cfg desc")

    asyncio.run(_run())


def test_provenance_absent_returns_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            prov = await read_build_config_provenance(conn, "nope")
        assert prov is None

    asyncio.run(_run())
