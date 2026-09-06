"""Migration 0137 indexes the general run-scoped jobs lookup from #2204."""

from __future__ import annotations

import psycopg

from kdive.db import migrate
from kdive.mcp.tools.external_boot.recovery_requests import _ACTIVE_JOBS_SQL

_INDEX = "jobs_payload_run_id_idx"


def test_0137_is_discovered_at_the_reserved_version() -> None:
    migration = next(item for item in migrate.discover_migrations() if item.version == "0137")
    assert migration.filename == "0137_jobs_payload_run_id_index.sql"


def test_0137_creates_the_run_id_expression_index(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = %s::regclass",
        (_INDEX,),
    ).fetchone()
    assert row is not None
    assert "((payload ->> 'run_id'::text))" in row[0]


def test_active_jobs_query_keeps_two_bounded_arms_without_global_ordering() -> None:
    assert _ACTIVE_JOBS_SQL.count("SELECT j.id FROM jobs j") == 2
    assert "UNION" in _ACTIVE_JOBS_SQL
    assert "ORDER BY" not in _ACTIVE_JOBS_SQL
    assert "payload->>'system_id'" in _ACTIVE_JOBS_SQL
    assert "payload->>'run_id'" in _ACTIVE_JOBS_SQL
