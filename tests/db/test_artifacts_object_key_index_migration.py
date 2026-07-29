"""Migration 0081 adds a general btree index on artifacts.object_key (#1570)."""

from __future__ import annotations

from uuid import uuid4

import psycopg

from kdive.db import migrate


def _apply_before(conn: psycopg.Connection, version: str) -> None:
    for m in migrate.discover_migrations():
        if m.version >= version:
            break
        conn.execute(m.sql.encode())  # bytes: a dynamic str fails ty (see migrate.py)


def _insert_artifact(conn: psycopg.Connection, owner_kind: str, key: str) -> None:
    conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES (%s, %s, %s, 'e', 'redacted', 'console')",
        (owner_kind, uuid4(), key),
    )


def test_pre_existing_no_general_object_key_index(pg_conn: psycopg.Connection) -> None:
    """Before 0081, no unpartitioned index names object_key."""
    _apply_before(pg_conn, "0081")
    row = pg_conn.execute(
        "SELECT 1 FROM pg_indexes "
        "WHERE tablename = 'artifacts' AND indexname = 'artifacts_object_key_idx'"
    ).fetchone()
    assert row is None


def test_general_object_key_index_exists(pg_conn: psycopg.Connection) -> None:
    """After migration, a plain (non-partial) btree index on object_key exists."""
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'artifacts_object_key_idx'"
    ).fetchone()
    assert row is not None
    indexdef = row[0]
    assert "object_key" in indexdef
    assert "WHERE" not in indexdef.upper()


def test_pre_existing_partial_unique_index_still_present(pg_conn: psycopg.Connection) -> None:
    """The 0076 partial unique index on object_key survives untouched (ADR-0441 §3)."""
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'artifacts_investigations_object_key_uniq'"
    ).fetchone()
    assert row is not None
    indexdef = row[0]
    assert "UNIQUE" in indexdef.upper()
    assert "owner_kind" in indexdef


def test_general_index_is_usable_without_an_owner_kind_predicate(
    pg_conn: psycopg.Connection,
) -> None:
    """The general index is usable by a query with no owner_kind predicate.

    This is the sweep's actual shape (`_RECLAIMABLE_SQL`): a plain equality lookup on
    object_key alone, with no owner_kind filter the 0076 partial index could match against.
    The test table is far too small for the planner to *prefer* an index scan on cost alone,
    so this forces sequential scan off and asserts the general index is a legal plan for this
    predicate shape — before 0081 that forced plan would have had no index to fall back to
    other than a full-table scan disguised as an index-only scan on an unrelated column.
    """
    migrate.apply_migrations(pg_conn)
    _insert_artifact(pg_conn, "runs", "local/runs/some-run/present.img")
    pg_conn.execute("ANALYZE artifacts")

    pg_conn.execute("SET enable_seqscan = off")
    plan = pg_conn.execute(
        "EXPLAIN SELECT 1 FROM artifacts a WHERE a.object_key = 'local/runs/some-run/absent.img'"
    ).fetchall()
    plan_text = "\n".join(row[0] for row in plan)
    assert "artifacts_object_key_idx" in plan_text


def test_object_key_round_trips_for_non_investigation_owner(pg_conn: psycopg.Connection) -> None:
    """A runs-owned artifact keeps writing object_key normally after the migration."""
    migrate.apply_migrations(pg_conn)
    _insert_artifact(pg_conn, "runs", "local/runs/some-run/present.img")

    row = pg_conn.execute(
        "SELECT object_key FROM artifacts WHERE object_key = %s",
        ("local/runs/some-run/present.img",),
    ).fetchone()
    assert row is not None
    assert row[0] == "local/runs/some-run/present.img"
