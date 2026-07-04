"""Migration 0060 adds a nullable image_catalog.description column (#1017, ADR-0311)."""

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


def _description_column(conn: psycopg.Connection) -> tuple[str, str] | None:
    """Return (data_type, is_nullable) for image_catalog.description, or None if absent."""
    row = conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'image_catalog' AND column_name = 'description'"
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def test_pre_migration_0060_has_no_description_column(pg_conn: psycopg.Connection) -> None:
    """Before 0060 lands, image_catalog carries no description column."""
    _apply_through(pg_conn, "0059")
    assert _description_column(pg_conn) is None


def test_migration_0060_adds_nullable_text_description(pg_conn: psycopg.Connection) -> None:
    """After all migrations, image_catalog.description is a nullable text column."""
    migrate.apply_migrations(pg_conn)
    assert _description_column(pg_conn) == ("text", "YES")
