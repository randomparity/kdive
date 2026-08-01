"""Migration 0081 adds a general btree index on artifacts.object_key (#1570)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg

from kdive.db import migrate
from kdive.reconciler.cleanup.upload_fences import _RECLAIMABLE_SQL
from kdive.store.objectstore import _LIST_PAGE_SIZE as _PAGE_WIDTH


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
    assert any(
        index in plan_text for index in ("artifacts_object_key_idx", "artifacts_owner_triple_uniq")
    )


def test_the_sweeps_page_wide_classify_is_index_served(pg_conn: psycopg.Connection) -> None:
    """The orphan sweep classifies a listing page at a time now (#1569, ADR-0498 §5).

    Paging turns one root-wide statement into several page-wide ones, and the worry that makes
    that worth pinning is the planner picking a *worse* strategy at the narrower width: the
    driving side's estimate tracks the array's real length, so the width is visible to the
    planner and the choice genuinely can differ. It differs in the safe direction — at a page's
    width the ``artifacts`` anti-join is a nested loop over migration ``0081``'s ``object_key``
    btree, so N pages cost N pages' worth of index probes and not N sequential scans.

    ``_RECLAIMABLE_SQL`` itself is what is explained, not a hand-written approximation of it. The
    real statement has four parallel arrays, a ``last_modified`` inequality that cuts the driving
    estimate to a third, and a second anti-join against ``upload_manifests`` — all of which move
    the join order search and the crossover, so a reduced stand-in would pin a plan the sweep
    never asks for.

    Enough rows are inserted for the index to win on cost, so no ``enable_seqscan`` override is
    needed and the plan asserted is the one production gets. ADR-0498 §5 records the measurement
    across widths and the wall-clock comparison against the root-wide statement.
    """
    migrate.apply_migrations(pg_conn)
    owner = uuid4()
    pg_conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) SELECT 'runs', %s, 'local/runs/' || %s || '/artifact-' || i, "
        "'e', 'redacted', 'console' FROM generate_series(1, 200000) AS i",
        (owner, str(owner)),
    )
    pg_conn.execute("ANALYZE artifacts")

    aged = datetime.now(UTC) - timedelta(days=99)
    page = [f"local/runs/{owner}/absent-{i:06d}" for i in range(_PAGE_WIDTH)]
    plan = pg_conn.execute(
        b"EXPLAIN " + _RECLAIMABLE_SQL.encode(),
        (page, [aged] * len(page), ["runs"] * len(page), [owner] * len(page), timedelta(hours=1)),
    ).fetchall()
    plan_text = "\n".join(row[0] for row in plan)
    assert "artifacts_object_key_idx" in plan_text, plan_text
    assert "Seq Scan on artifacts" not in plan_text, plan_text


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
