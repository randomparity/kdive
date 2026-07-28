"""Migration 0080 resolves stranded `defined` Systems and retires the state (#1600, ADR-0457)."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db import migrate
from kdive.domain.capacity.state import SystemState
from kdive.security.audit import args_digest

_MIGRATION = "0080"


def _apply_before(conn: psycopg.Connection, version: str) -> None:
    for m in migrate.discover_migrations():
        if m.version >= version:
            break
        conn.execute(m.sql.encode())  # bytes: a dynamic str fails ty (see migrate.py:135-138)


def _apply_version(conn: psycopg.Connection, version: str) -> None:
    sql = next(m.sql for m in migrate.discover_migrations() if m.version == version)
    conn.execute(sql.encode())  # bytes: a dynamic str fails ty (see migrate.py:135-138)


def _seed_allocation(conn: psycopg.Connection) -> UUID:
    """Insert the resource + `active` allocation FK chain a System needs."""
    resource_id, allocation_id = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    return allocation_id


def _seed_system(conn: psycopg.Connection, state: str, project: str = "proj") -> UUID:
    system_id = uuid4()
    conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'p', %s)",
        (system_id, _seed_allocation(conn), state, project),
    )
    return system_id


def _state(conn: psycopg.Connection, system_id: UUID) -> str:
    row = conn.execute("SELECT state FROM systems WHERE id = %s", (system_id,)).fetchone()
    assert row is not None
    return str(row[0])


def _allocation_state(conn: psycopg.Connection, system_id: UUID) -> str:
    row = conn.execute(
        "SELECT a.state FROM allocations a JOIN systems s ON s.allocation_id = a.id "
        "WHERE s.id = %s",
        (system_id,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _audit_rows(conn: psycopg.Connection, system_id: UUID) -> list[tuple[str, str, str, str]]:
    rows = conn.execute(
        "SELECT principal, project, tool, args_digest FROM audit_log "
        "WHERE object_kind = 'systems' AND object_id = %s AND transition = 'defined->torn_down'",
        (system_id,),
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]


def test_0080_resolves_defined_rows_to_torn_down(pg_conn: psycopg.Connection) -> None:
    """A stranded `defined` System lands in `torn_down`, the terminal state that frees its slot.

    `torn_down` (not `failed`) because no provider work ever started: there is no host domain to
    reap and no error to report, only a `max_concurrent_systems` reservation to give back.
    """
    _apply_before(pg_conn, _MIGRATION)
    system_id = _seed_system(pg_conn, "defined")

    _apply_version(pg_conn, _MIGRATION)

    assert _state(pg_conn, system_id) == SystemState.TORN_DOWN.value


def test_0080_leaves_every_other_state_untouched(pg_conn: psycopg.Connection) -> None:
    """Only `defined` rows are rewritten; a live or already-terminal System is not disturbed."""
    _apply_before(pg_conn, _MIGRATION)
    others = {
        state: _seed_system(pg_conn, state)
        for state in ("provisioning", "ready", "crashed", "failed", "torn_down")
    }

    _apply_version(pg_conn, _MIGRATION)

    assert {state: _state(pg_conn, sid) for state, sid in others.items()} == {
        state: state for state in others
    }


def test_0080_leaves_the_allocation_active_for_the_reconciler(
    pg_conn: psycopg.Connection,
) -> None:
    """The Allocation stays `active`: `reap_orphaned_active_allocations` owns the release.

    The migration deliberately does not release it in SQL — that repair re-checks the
    no-live-System predicate under the PROJECT -> ALLOCATION lock and writes the release audit
    trail and ledger credit (ADR-0109) that a bare UPDATE here could not.
    """
    _apply_before(pg_conn, _MIGRATION)
    system_id = _seed_system(pg_conn, "defined")

    _apply_version(pg_conn, _MIGRATION)

    assert _allocation_state(pg_conn, system_id) == "active"


def test_0080_writes_one_audit_row_per_resolved_system(pg_conn: psycopg.Connection) -> None:
    """Each resolution is audited so the object's trail has no silent gap across the deploy."""
    _apply_before(pg_conn, _MIGRATION)
    system_id = _seed_system(pg_conn, "defined", project="other-proj")

    _apply_version(pg_conn, _MIGRATION)

    assert _audit_rows(pg_conn, system_id) == [
        (
            "system:migration",
            "other-proj",
            "migration:0080_retire_defined_system_state",
            args_digest({}),
        )
    ]


def test_0080_audits_nothing_when_no_defined_rows_exist(pg_conn: psycopg.Connection) -> None:
    """The empty case — the common one on a fresh DB — writes no audit rows at all."""
    _apply_before(pg_conn, _MIGRATION)
    _seed_system(pg_conn, "ready")

    _apply_version(pg_conn, _MIGRATION)

    row = pg_conn.execute(
        "SELECT count(*) FROM audit_log WHERE transition = 'defined->torn_down'"
    ).fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_0080_constraint_rejects_a_new_defined_row(pg_conn: psycopg.Connection) -> None:
    """`defined` is gone from the CHECK, so the DB refuses a value the code can no longer read."""
    _apply_before(pg_conn, _MIGRATION)
    allocation_id = _seed_allocation(pg_conn)

    _apply_version(pg_conn, _MIGRATION)

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO systems (allocation_id, state, provisioning_profile, principal, "
            "project) VALUES (%s, 'defined', '{}'::jsonb, 'p', 'proj')",
            (allocation_id,),
        )


def test_0080_constraint_admits_exactly_the_enum_values(pg_conn: psycopg.Connection) -> None:
    """Closes the direction CHECK_ENUMS cannot: no SQL-only value survives the tighten."""
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'systems_state_check'"
    ).fetchone()
    assert row is not None, "systems_state_check constraint is missing"
    admitted = set(re.findall(r"'([^']+)'", row[0]))
    assert admitted == {s.value for s in SystemState}
