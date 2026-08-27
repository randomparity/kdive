"""Migration 0119 removes the cursor table retired by the split GC lanes."""

from __future__ import annotations

import psycopg

from kdive.db import migrate


def test_0119_removes_obsolete_build_gc_cursor_table(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)

    row = pg_conn.execute("SELECT to_regclass('public.build_artifact_gc_cursors')").fetchone()

    assert row == (None,)
    assert pg_conn.execute(
        "SELECT to_regclass('public.investigation_build_gc_cursor'), "
        "to_regclass('public.system_object_sweep_cursors')"
    ).fetchone() == ("investigation_build_gc_cursor", "system_object_sweep_cursors")
