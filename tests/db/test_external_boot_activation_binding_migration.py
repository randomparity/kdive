"""Migration 0124 activation-binding persistence proofs (ADR-0586)."""

from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
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


def test_migration_replaces_only_recovery_ownership_and_preserves_grants(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0123")
    before = pg_conn.execute(
        "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
        "WHERE table_name = 'external_boot_activations' ORDER BY 1, 2"
    ).fetchall()
    migration = next(item for item in migrate.discover_migrations() if item.version == "0124")
    pg_conn.execute(migration.sql.encode())
    definition = _constraint(pg_conn)

    assert "binding,system_id" in definition
    assert "binding,run_id" in definition
    assert "binding,activation_id" in definition
    assert "ownership,system_id" in definition  # materialization arm remains
    assert (
        pg_conn.execute(
            "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
            "WHERE table_name = 'external_boot_activations' ORDER BY 1, 2"
        ).fetchall()
        == before
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


def test_legacy_preflight_aborts_without_dropping_old_check(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0123")
    system_id, run_id, activation_id = _seed(pg_conn)
    legacy = _point(system_id, run_id, activation_id)
    legacy["ownership"] = legacy.pop("binding")
    _insert(pg_conn, system_id, run_id, activation_id, legacy)
    pg_conn.commit()
    before = _constraint(pg_conn)
    migration = next(item for item in migrate.discover_migrations() if item.version == "0124")

    with (
        pytest.raises(psycopg.errors.RaiseException, match="incompatible persisted"),
        pg_conn.transaction(),
    ):
        pg_conn.execute(migration.sql.encode())

    assert _constraint(pg_conn) == before
    assert "ownership,system_id" in before
    assert "binding,system_id" not in before
