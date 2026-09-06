"""Migration 0124 activation-binding persistence proofs (ADR-0586)."""

from __future__ import annotations

import hashlib
from typing import LiteralString, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from kdive.db import migrate

_PLAN = "sha256:" + "a" * 64


def _apply_through(conn: psycopg.Connection, version: str) -> None:
    for migration in migrate.discover_migrations():
        if migration.version <= version:
            conn.execute(migration.sql.encode())


def _seed(conn: psycopg.Connection) -> tuple[UUID, UUID, UUID]:
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
        "principal, project) VALUES "
        "(%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )
    return system_id, run_id, uuid4()


def _materialization(system_id: UUID, run_id: UUID) -> dict[str, object]:
    return {
        "schema": "external-boot-materialization-v1",
        "ownership": {"system_id": str(system_id), "run_id": str(run_id)},
        "plan_identity": _PLAN,
    }


def _point(system_id: UUID, run_id: UUID, activation_id: UUID) -> dict[str, object]:
    return {
        "schema": "external-boot-recovery-v1",
        "binding": {
            "system_id": str(system_id),
            "run_id": str(run_id),
            "activation_id": str(activation_id),
        },
        "plan_identity": _PLAN,
    }


def _insert(
    conn: psycopg.Connection,
    system_id: UUID,
    run_id: UUID,
    activation_id: UUID,
    point: object,
) -> None:
    conn.execute(
        "INSERT INTO external_boot_activations "
        "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
        "state, materialization, recovery_point) "
        "VALUES (%s, %s, %s, %s, %s, 1, 'prepared', %s, %s)",
        (
            activation_id,
            system_id,
            run_id,
            _PLAN,
            uuid4(),
            Jsonb(_materialization(system_id, run_id)),
            Jsonb(point),
        ),
    )


def _constraint(conn: psycopg.Connection) -> str:
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid, true) FROM pg_constraint "
        "WHERE conrelid = 'external_boot_activations'::regclass "
        "AND conname = 'external_boot_activation_evidence_ownership'"
    ).fetchone()
    assert row is not None
    return row[0]


def _install_old_check_not_valid(conn: psycopg.Connection, definition: str) -> None:
    conn.execute(
        sql.SQL(
            "ALTER TABLE external_boot_activations "
            "ADD CONSTRAINT external_boot_activation_evidence_ownership {} NOT VALID"
        ).format(sql.SQL(cast(LiteralString, definition)))
    )


def _schema_snapshot(conn: psycopg.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT 'relation', n.nspname, c.relname, c.relkind::text, '' FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT 'constraint', n.nspname, c.relname, con.conname, "
        "pg_get_constraintdef(con.oid, true) || ':' || con.convalidated::text "
        "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT 'function', n.nspname, p.proname, "
        "pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid) "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' ORDER BY 1, 2, 3, 4, 5"
    ).fetchall()


def _grant_snapshot(conn: psycopg.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT grantee, table_schema, table_name, privilege_type, is_grantable "
        "FROM information_schema.role_table_grants ORDER BY 1, 2, 3, 4, 5"
    ).fetchall()


def test_migration_replaces_only_recovery_ownership_and_preserves_grants(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0123")
    system_id, run_id, activation_id = _seed(pg_conn)
    pg_conn.execute(
        "INSERT INTO external_boot_activations "
        "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
        "state, materialization) VALUES (%s, %s, %s, %s, %s, 1, 'preparing', %s)",
        (
            activation_id,
            system_id,
            run_id,
            _PLAN,
            uuid4(),
            Jsonb(_materialization(system_id, run_id)),
        ),
    )
    rows_before = pg_conn.execute(
        "SELECT id, materialization::text, recovery_point::text "
        "FROM external_boot_activations ORDER BY id"
    ).fetchall()
    grants_before = _grant_snapshot(pg_conn)
    objects_before = _schema_snapshot(pg_conn)
    migration = next(item for item in migrate.discover_migrations() if item.version == "0124")
    pg_conn.execute(migration.sql.encode())
    definition = _constraint(pg_conn)
    expected_definition_digest = (
        "170315f062cfe27a9ea17052ee0e0b8e"  # pragma: allowlist secret
        "fc9f9d3dbf72ea37ce33de54df6de646"  # pragma: allowlist secret
    )
    assert hashlib.sha256(definition.encode()).hexdigest() == expected_definition_digest

    assert "binding,system_id" in definition
    assert "binding,run_id" in definition
    assert "binding,activation_id" in definition
    assert "ownership,system_id" in definition  # materialization arm remains
    assert "jsonb_typeof(recovery_point #> '{binding,system_id}'" in definition
    assert "CREATE ROLE" not in migration.sql.upper()
    assert "ALTER ROLE" not in migration.sql.upper()
    assert "DROP ROLE" not in migration.sql.upper()
    assert _grant_snapshot(pg_conn) == grants_before
    after_objects = _schema_snapshot(pg_conn)
    assert [
        row for row in after_objects if row[3] != "external_boot_activation_evidence_ownership"
    ] == [row for row in objects_before if row[3] != "external_boot_activation_evidence_ownership"]
    assert (
        pg_conn.execute(
            "SELECT id, materialization::text, recovery_point::text "
            "FROM external_boot_activations ORDER BY id"
        ).fetchall()
        == rows_before
    )


def test_canonical_binding_persists_and_legacy_ownership_is_rejected(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0124")
    system_id, run_id, activation_id = _seed(pg_conn)
    _insert(pg_conn, system_id, run_id, activation_id, _point(system_id, run_id, activation_id))

    legacy = _point(system_id, run_id, uuid4())
    legacy["ownership"] = legacy.pop("binding")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, system_id, run_id, uuid4(), legacy)


@pytest.mark.parametrize(
    "case",
    [
        "missing-binding",
        "scalar-binding",
        "missing-key",
        "extra-key",
        "malformed-uuid",
        "cross-system",
        "cross-run",
        "cross-activation",
    ],
)
def test_replacement_check_rejects_malformed_or_extra_binding(
    pg_conn: psycopg.Connection, case: str
) -> None:
    _apply_through(pg_conn, "0124")
    system_id, run_id, activation_id = _seed(pg_conn)
    point = _point(system_id, run_id, activation_id)
    binding = cast(dict[str, object], point["binding"])
    if case == "missing-binding":
        point.pop("binding")
    elif case == "scalar-binding":
        point["binding"] = []
    elif case == "missing-key":
        binding.pop("activation_id")
    elif case == "extra-key":
        binding["extra"] = "value"
    elif case == "malformed-uuid":
        binding["activation_id"] = "not-a-uuid"
    elif case == "cross-system":
        binding["system_id"] = str(uuid4())
    elif case == "cross-run":
        binding["run_id"] = str(uuid4())
    else:
        binding["activation_id"] = str(uuid4())
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.InvalidTextRepresentation)):
        _insert(pg_conn, system_id, run_id, activation_id, point)


def test_reachable_legacy_preflight_aborts_without_partial_ddl(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0123")
    system_id, run_id, activation_id = _seed(pg_conn)
    point = _point(system_id, run_id, activation_id)
    point["ownership"] = point.pop("binding")
    before = _constraint(pg_conn)
    _insert(pg_conn, system_id, run_id, activation_id, point)
    pg_conn.commit()
    installed_check = _constraint(pg_conn)
    ddl_before = _schema_snapshot(pg_conn)
    rows_before = pg_conn.execute(
        "SELECT id, recovery_point::text FROM external_boot_activations ORDER BY id"
    ).fetchall()
    migration = next(item for item in migrate.discover_migrations() if item.version == "0124")

    with (
        pytest.raises(psycopg.errors.RaiseException, match="incompatible persisted"),
        pg_conn.transaction(),
    ):
        pg_conn.execute(migration.sql.encode())

    assert _constraint(pg_conn) == installed_check
    assert _schema_snapshot(pg_conn) == ddl_before
    assert (
        pg_conn.execute(
            "SELECT id, recovery_point::text FROM external_boot_activations ORDER BY id"
        ).fetchall()
        == rows_before
    )
    assert "ownership,system_id" in before
    assert "binding,system_id" not in before


def test_not_valid_0121_definition_is_rejected_without_partial_ddl(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0123")
    before = _constraint(pg_conn)
    pg_conn.execute(
        "ALTER TABLE external_boot_activations "
        "DROP CONSTRAINT external_boot_activation_evidence_ownership"
    )
    _install_old_check_not_valid(pg_conn, before)
    pg_conn.commit()
    installed_check = _constraint(pg_conn)
    ddl_before = _schema_snapshot(pg_conn)
    rows_before = pg_conn.execute(
        "SELECT id, recovery_point::text FROM external_boot_activations ORDER BY id"
    ).fetchall()
    migration = next(item for item in migrate.discover_migrations() if item.version == "0124")

    with (
        pytest.raises(psycopg.errors.RaiseException, match="exact migration 0121"),
        pg_conn.transaction(),
    ):
        pg_conn.execute(migration.sql.encode())

    assert _constraint(pg_conn) == installed_check
    assert _schema_snapshot(pg_conn) == ddl_before
    assert (
        pg_conn.execute(
            "SELECT id, recovery_point::text FROM external_boot_activations ORDER BY id"
        ).fetchall()
        == rows_before
    )


def test_modified_non_recovery_arm_aborts_before_drop(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0123")
    pg_conn.execute(
        "ALTER TABLE external_boot_activations "
        "DROP CONSTRAINT external_boot_activation_evidence_ownership"
    )
    pg_conn.execute(
        "ALTER TABLE external_boot_activations "
        "ADD CONSTRAINT external_boot_activation_evidence_ownership CHECK (true)"
    )
    pg_conn.commit()
    before = _constraint(pg_conn)
    ddl_before = _schema_snapshot(pg_conn)
    migration = next(item for item in migrate.discover_migrations() if item.version == "0124")

    with (
        pytest.raises(psycopg.errors.RaiseException, match="exact migration 0121"),
        pg_conn.transaction(),
    ):
        pg_conn.execute(migration.sql.encode())

    assert _constraint(pg_conn) == before
    assert _schema_snapshot(pg_conn) == ddl_before
