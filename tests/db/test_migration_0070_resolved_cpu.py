"""Migration 0070 adds a nullable systems.resolved_cpu jsonb column (#980, ADR-0368)."""

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


def _resolved_cpu_column(conn: psycopg.Connection) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'systems' AND column_name = 'resolved_cpu'"
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def test_pre_migration_0070_has_no_resolved_cpu_column(pg_conn: psycopg.Connection) -> None:
    """Before 0070 lands, the systems table carries no resolved_cpu column."""
    _apply_through(pg_conn, "0069")
    assert _resolved_cpu_column(pg_conn) is None


def test_migration_0070_adds_nullable_jsonb_column(pg_conn: psycopg.Connection) -> None:
    """After all migrations, resolved_cpu is a nullable jsonb column."""
    migrate.apply_migrations(pg_conn)
    assert _resolved_cpu_column(pg_conn) == ("jsonb", "YES")
