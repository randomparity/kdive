"""Migration 0064 adds image_catalog.provenance_attested (bool, NOT NULL, default false)."""

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


def _attested_column(conn: psycopg.Connection) -> tuple[str, str, str | None] | None:
    """Return (data_type, is_nullable, column_default) for provenance_attested, or None."""
    row = conn.execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'image_catalog' AND column_name = 'provenance_attested'"
    ).fetchone()
    return (row[0], row[1], row[2]) if row is not None else None


def test_pre_migration_0064_has_no_provenance_attested_column(pg_conn: psycopg.Connection) -> None:
    """Before 0064 lands, image_catalog carries no provenance_attested column."""
    _apply_through(pg_conn, "0063")
    assert _attested_column(pg_conn) is None


def test_migration_0064_adds_not_null_boolean_default_false(pg_conn: psycopg.Connection) -> None:
    """After all migrations, provenance_attested is a NOT NULL boolean defaulting to false."""
    migrate.apply_migrations(pg_conn)
    column = _attested_column(pg_conn)
    assert column is not None
    data_type, is_nullable, column_default = column
    assert data_type == "boolean"
    assert is_nullable == "NO"
    assert column_default is not None and "false" in column_default.lower()
