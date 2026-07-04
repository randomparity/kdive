"""Migration 0061 seeds the curated 'debug' system shape (#985, ADR-0312)."""

from __future__ import annotations

import psycopg

from kdive.db import migrate


def _apply_through(conn: psycopg.Connection, last_version: str) -> None:
    """Apply migrations up to and including last_version without the migration runner."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    text PRIMARY KEY,
            filename   text NOT NULL,
            checksum   text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    for m in migrate.discover_migrations():
        if m.version > last_version:
            break
        conn.execute(m.sql.encode())
        conn.execute(
            "INSERT INTO schema_migrations (version, filename, checksum) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (m.version, m.filename, m.checksum),
        )


def _debug_shape(conn: psycopg.Connection) -> tuple[int, int, int] | None:
    row = conn.execute(
        "SELECT vcpus, memory_mb, disk_gb FROM system_shapes WHERE name = 'debug'"
    ).fetchone()
    return (row[0], row[1], row[2]) if row is not None else None


def test_pre_migration_0061_has_no_debug_shape(pg_conn: psycopg.Connection) -> None:
    """Before 0061 lands, the shape catalog carries no 'debug' preset."""
    _apply_through(pg_conn, "0060")
    assert _debug_shape(pg_conn) is None


def test_migration_0061_seeds_debug_shape(pg_conn: psycopg.Connection) -> None:
    """After all migrations, the 'debug' preset is 4 vcpu / 8 GB / 60 GB."""
    migrate.apply_migrations(pg_conn)
    assert _debug_shape(pg_conn) == (4, 8192, 60)
