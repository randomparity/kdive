"""Migration 0054 adds a nullable run_id correlation column to artifacts (ADR-0279, #935)."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg

from kdive.db import migrate


def _apply_before(conn: psycopg.Connection, version: str) -> None:
    for m in migrate.discover_migrations():
        if m.version >= version:
            break
        conn.execute(m.sql.encode())  # bytes: a dynamic str fails ty (see migrate.py)


def _seed_run(conn: psycopg.Connection) -> tuple[UUID, UUID]:
    """Insert the FK chain a Run needs; return (system_id, run_id)."""
    resource_id, allocation_id = uuid4(), uuid4()
    system_id, investigation_id, run_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'granted', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, 'ready', '{}'::jsonb, 'p', 'proj')",
        (system_id, allocation_id),
    )
    conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (investigation_id,),
    )
    conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) "
        "VALUES (%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )
    return system_id, run_id


def _one(row: tuple[object, ...] | None) -> object:
    assert row is not None
    return row[0]


def _insert_artifact(
    conn: psycopg.Connection, system_id: UUID, key: str, run_id: UUID | None
) -> UUID:
    row = conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class, run_id) VALUES ('systems', %s, %s, 'e', 'redacted', 'console', %s) "
        "RETURNING id",
        (system_id, key, run_id),
    ).fetchone()
    assert row is not None
    return row[0]


def test_pre_existing_artifacts_have_no_run_id_column(pg_conn: psycopg.Connection) -> None:
    """Before 0054, the artifacts table has no run_id column."""
    _apply_before(pg_conn, "0054")
    row = pg_conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'artifacts' AND column_name = 'run_id'"
    ).fetchone()
    assert row is None


def test_run_id_round_trips_and_defaults_null(pg_conn: psycopg.Connection) -> None:
    """After migration, run_id round-trips and an unstamped insert reads NULL."""
    migrate.apply_migrations(pg_conn)
    system_id, run_id = _seed_run(pg_conn)

    correlated = _insert_artifact(pg_conn, system_id, "console-part-0-000000", run_id)
    uncorrelated = _insert_artifact(pg_conn, system_id, "dmesg-redacted", None)

    assert (
        _one(
            pg_conn.execute("SELECT run_id FROM artifacts WHERE id = %s", (correlated,)).fetchone()
        )
        == run_id
    )
    assert (
        _one(
            pg_conn.execute(
                "SELECT run_id FROM artifacts WHERE id = %s", (uncorrelated,)
            ).fetchone()
        )
        is None
    )


def test_partial_index_exists(pg_conn: psycopg.Connection) -> None:
    """The partial index on run_id (WHERE run_id IS NOT NULL) is created."""
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'artifacts_run_id_idx'"
    ).fetchone()
    assert row is not None
    assert "run_id" in row[0]
    assert "IS NOT NULL" in row[0].upper() or "is not null" in row[0]


def test_run_id_fk_rejects_unknown_run(pg_conn: psycopg.Connection) -> None:
    """run_id is a real FK: a console artifact cannot name a non-existent Run."""
    migrate.apply_migrations(pg_conn)
    system_id, _ = _seed_run(pg_conn)
    try:
        _insert_artifact(pg_conn, system_id, "console-part-0-000001", uuid4())
    except psycopg.errors.ForeignKeyViolation:
        return
    raise AssertionError("expected a ForeignKeyViolation for an unknown run_id")
