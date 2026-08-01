"""Migration 0091 persists fair scan positions for rowless System objects (ADR-0524, #1751)."""

from __future__ import annotations

import re

import psycopg
import pytest

from kdive.db import migrate


def test_0091_is_discovered_after_0090() -> None:
    migrations = migrate.discover_migrations()

    assert [(item.version, item.filename) for item in migrations[-3:]] == [
        ("0092", "0092_image_publication_attempt.sql"),
        ("0093", "0093_image_publication_attempt_contract.sql"),
        ("0094", "0094_artifact_owner_triple_unique.sql"),
    ]


def test_0091_seeds_exactly_the_three_sweep_lanes(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)

    rows = pg_conn.execute(
        "SELECT lane, after_key FROM system_object_sweep_cursors ORDER BY lane"
    ).fetchall()

    assert rows == [("local", None), ("remote", None), ("row-backed", None)]


def test_0091_lane_check_accepts_only_declared_system_cleanup_lanes(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)

    constraint = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'system_object_sweep_cursors'::regclass AND contype = 'c'"
    ).fetchone()
    assert constraint is not None
    assert set(re.findall(r"'([^']+)'", str(constraint[0]))) == {
        "local",
        "remote",
        "row-backed",
    }

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute("INSERT INTO system_object_sweep_cursors (lane) VALUES ('third-lane')")


def test_0091_cursor_columns_have_the_required_nullability(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)

    rows = pg_conn.execute(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'system_object_sweep_cursors' "
        "ORDER BY ordinal_position"
    ).fetchall()

    assert rows[0][:3] == ("lane", "text", "NO")
    assert rows[1] == ("after_key", "text", "YES", None)
    assert rows[2][0:3] == ("updated_at", "timestamp with time zone", "NO")
    assert rows[2][3] is not None and "now()" in rows[2][3]
