"""Migration 0048 adds investigations.cleanup_pending_at and backfills closed rows (#768)."""

from __future__ import annotations

from uuid import uuid4

import psycopg

from kdive.db import migrate


def _apply_before(conn: psycopg.Connection, version: str) -> None:
    for m in migrate.discover_migrations():
        if m.version >= version:
            break
        conn.execute(m.sql.encode())  # bytes: a dynamic str fails ty (see migrate.py:135-138)


def _apply_version(conn: psycopg.Connection, version: str) -> None:
    sql = next(m.sql for m in migrate.discover_migrations() if m.version == version)
    conn.execute(sql.encode())  # bytes: a dynamic str fails ty (see migrate.py:135-138)


def _insert_investigation(conn: psycopg.Connection, inv_id: object, state: str) -> None:
    conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, %s, %s, %s, %s)",
        (inv_id, "p", "proj", "t", state),
    )


def _one(row: tuple[object, ...] | None) -> object:
    assert row is not None
    return row[0]


def test_migration_0048_backfills_closed_investigations(pg_conn: psycopg.Connection) -> None:
    _apply_before(pg_conn, "0048")
    open_id, closed_id = uuid4(), uuid4()
    _insert_investigation(pg_conn, open_id, "open")
    _insert_investigation(pg_conn, closed_id, "closed")
    closed_updated = _one(
        pg_conn.execute(
            "SELECT updated_at FROM investigations WHERE id = %s", (closed_id,)
        ).fetchone()
    )

    _apply_version(pg_conn, "0048")

    closed_marker = _one(
        pg_conn.execute(
            "SELECT cleanup_pending_at FROM investigations WHERE id = %s", (closed_id,)
        ).fetchone()
    )
    open_marker = _one(
        pg_conn.execute(
            "SELECT cleanup_pending_at FROM investigations WHERE id = %s", (open_id,)
        ).fetchone()
    )

    assert closed_marker == closed_updated
    assert open_marker is None
