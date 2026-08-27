"""Migration 0115 persists reap-once convergence for capture reclamation (ADR-0556, #1946)."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate

_RECONCILER_CUTOFF_COLUMNS = ("singleton", "complete", "cutoff_at")


def test_latest_migrations_are_discovered_in_order() -> None:
    migrations = migrate.discover_migrations()

    assert [(item.version, item.filename) for item in migrations[-4:]] == [
        ("0114", "0114_host_dump_volume_leases.sql"),
        ("0115", "0115_capture_reap_state.sql"),
        ("0116", "0116_capture_claimable_queue_depth.sql"),
        ("0117", "0117_worker_bootstrap_key_insert.sql"),
    ]


def _job(conn: psycopg.Connection) -> str:
    job_id = str(uuid4())
    conn.execute(
        "INSERT INTO jobs (id, kind, payload, state, max_attempts, authorizing, dedup_key) "
        "VALUES (%s, 'capture_traffic', %s, 'failed', 3, %s, %s)",
        (
            job_id,
            Jsonb({"run_id": str(uuid4())}),
            Jsonb({"principal": "p", "project": "project-a"}),
            job_id,
        ),
    )
    return job_id


def test_0115_reap_state_columns_have_the_required_nullability(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)

    rows = pg_conn.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'capture_reap_state' "
        "ORDER BY ordinal_position"
    ).fetchall()

    assert rows == [
        ("job_id", "uuid", "NO"),
        ("attempts", "integer", "NO"),
        ("retry_after", "timestamp with time zone", "YES"),
        ("reclaimed_at", "timestamp with time zone", "YES"),
        ("created_at", "timestamp with time zone", "NO"),
        ("updated_at", "timestamp with time zone", "NO"),
    ]


def test_0115_records_a_reclaimed_row_with_no_retry_deadline(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    job_id = _job(pg_conn)

    pg_conn.execute(
        "INSERT INTO capture_reap_state (job_id, attempts, reclaimed_at) VALUES (%s, 1, now())",
        (job_id,),
    )

    row = pg_conn.execute(
        "SELECT attempts, retry_after, reclaimed_at FROM capture_reap_state WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] is None
    assert row[2] is not None


def test_0115_records_a_deferred_row_with_a_deadline_and_a_spent_attempt(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    job_id = _job(pg_conn)

    pg_conn.execute(
        "INSERT INTO capture_reap_state (job_id, attempts, retry_after) "
        "VALUES (%s, 2, now() + interval '5 minutes')",
        (job_id,),
    )

    row = pg_conn.execute(
        "SELECT attempts, retry_after, reclaimed_at FROM capture_reap_state WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == 2
    assert row[1] is not None
    assert row[2] is None


def test_0115_rejects_a_row_that_is_neither_reclaimed_nor_scheduled(
    pg_conn: psycopg.Connection,
) -> None:
    """A row that says nothing would leave its job ineligible while recording no completion."""
    migrate.apply_migrations(pg_conn)
    job_id = _job(pg_conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute("INSERT INTO capture_reap_state (job_id) VALUES (%s)", (job_id,))


def test_0115_rejects_a_retry_deadline_on_a_reclaimed_row(pg_conn: psycopg.Connection) -> None:
    """Reclaimed rows are terminal, so a deadline on one is a contradiction."""
    migrate.apply_migrations(pg_conn)
    job_id = _job(pg_conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO capture_reap_state (job_id, attempts, retry_after, reclaimed_at) "
            "VALUES (%s, 1, now(), now())",
            (job_id,),
        )


def test_0115_rejects_a_deferred_row_that_spent_no_attempt(pg_conn: psycopg.Connection) -> None:
    """The attempt that deferred a row is itself spent, so a deferral at zero cannot exist."""
    migrate.apply_migrations(pg_conn)
    job_id = _job(pg_conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO capture_reap_state (job_id, attempts, retry_after) VALUES (%s, 0, now())",
            (job_id,),
        )


def test_0115_reap_state_is_removed_with_its_job(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    job_id = _job(pg_conn)
    pg_conn.execute(
        "INSERT INTO capture_reap_state (job_id, attempts, reclaimed_at) VALUES (%s, 1, now())",
        (job_id,),
    )

    pg_conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))

    remaining = pg_conn.execute("SELECT count(*) FROM capture_reap_state").fetchone()
    assert remaining == (0,)


def test_0115_reap_state_requires_a_real_job(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_conn.execute(
            "INSERT INTO capture_reap_state (job_id, attempts, reclaimed_at) VALUES (%s, 1, now())",
            (str(uuid4()),),
        )


def test_0115_grants_the_reconciler_the_writes_the_sweep_makes(
    pg_conn: psycopg.Connection,
) -> None:
    """Only the reconciler writes reap state; the server reads it for support."""
    migrate.apply_migrations(pg_conn)

    granted: set[tuple[str, str]] = set()
    for role in ("kdive_reconciler", "kdive_server", "kdive_worker", "kdive_lifecycle_witness"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            row = pg_conn.execute(
                "SELECT has_table_privilege(%s, 'public.capture_reap_state', %s)",
                (role, privilege),
            ).fetchone()
            assert row is not None
            if row[0]:
                granted.add((role, privilege))

    assert granted == {
        ("kdive_reconciler", "SELECT"),
        ("kdive_reconciler", "INSERT"),
        ("kdive_reconciler", "UPDATE"),
        ("kdive_server", "SELECT"),
    }


def test_0115_lets_the_reconciler_read_the_cutover_generation_by_column(
    pg_conn: psycopg.Connection,
) -> None:
    """The pre-cutover evidence path needs the cutoff; it stays a column grant, not a table one."""
    migrate.apply_migrations(pg_conn)

    for column in _RECONCILER_CUTOFF_COLUMNS:
        readable = pg_conn.execute(
            "SELECT has_column_privilege("
            "'kdive_reconciler', 'public.capture_operation_cutoff', %s, 'SELECT')",
            (column,),
        ).fetchone()
        assert readable == (True,), column

    assert pg_conn.execute(
        "SELECT has_table_privilege("
        "'kdive_reconciler', 'public.capture_operation_cutoff', 'SELECT')"
    ).fetchone() == (False,)
    assert pg_conn.execute(
        "SELECT has_column_privilege("
        "'kdive_reconciler', 'public.capture_operation_cutoff', 'operation_quiescent', 'SELECT')"
    ).fetchone() == (False,)
