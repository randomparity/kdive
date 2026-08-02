"""Migration 0102 persists cursors for every public build-GC lane (#1519)."""

from __future__ import annotations

import psycopg

from kdive.db import migrate


def test_0102_seeds_public_build_gc_lanes(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)

    rows = pg_conn.execute(
        "SELECT lane, after_id FROM build_artifact_gc_cursors ORDER BY lane"
    ).fetchall()

    assert rows == [
        ("closed-investigations", None),
        ("closed-legacy-artifacts", None),
        ("expired-legacy-artifacts", None),
    ]


def test_0102_is_latest_migration() -> None:
    migration = migrate.discover_migrations()[-1]

    assert (migration.version, migration.filename) == (
        "0102",
        "0102_build_artifact_gc_cursors.sql",
    )
