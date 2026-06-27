"""Migration 0050 adds nullable label to runs and systems (ADR-0264, #867)."""

from __future__ import annotations

from uuid import UUID, uuid4

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


def _seed_chain(conn: psycopg.Connection) -> tuple[UUID, UUID]:
    """Insert the FK chain a System and Run need; return (system_id, investigation_id)."""
    resource_id, allocation_id = uuid4(), uuid4()
    system_id, investigation_id = uuid4(), uuid4()
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
    return system_id, investigation_id


def _one(row: tuple[object, ...] | None) -> object:
    assert row is not None
    return row[0]


def test_pre_existing_rows_read_label_null(pg_conn: psycopg.Connection) -> None:
    _apply_before(pg_conn, "0050")
    system_id, investigation_id = _seed_chain(pg_conn)
    run_id = uuid4()
    pg_conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) "
        "VALUES (%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )

    _apply_version(pg_conn, "0050")

    assert (
        _one(pg_conn.execute("SELECT label FROM systems WHERE id = %s", (system_id,)).fetchone())
        is None
    )
    assert (
        _one(pg_conn.execute("SELECT label FROM runs WHERE id = %s", (run_id,)).fetchone()) is None
    )


def test_label_round_trips_on_runs_and_systems(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    resource_id, allocation_id = uuid4(), uuid4()
    system_id, investigation_id, run_id = uuid4(), uuid4(), uuid4()
    pg_conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    pg_conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'granted', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    pg_conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project, "
        "label) VALUES (%s, %s, 'ready', '{}'::jsonb, 'p', 'proj', %s)",
        (system_id, allocation_id, "my-system"),
    )
    pg_conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (investigation_id,),
    )
    pg_conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project, label) "
        "VALUES (%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj', %s)",
        (run_id, investigation_id, system_id, "my-run"),
    )

    assert (
        _one(pg_conn.execute("SELECT label FROM systems WHERE id = %s", (system_id,)).fetchone())
        == "my-system"
    )
    assert (
        _one(pg_conn.execute("SELECT label FROM runs WHERE id = %s", (run_id,)).fetchone())
        == "my-run"
    )
