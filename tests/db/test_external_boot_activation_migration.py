"""Migration 0121 external-boot activation invariants (ADR-0583/0584)."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb


def _seed_run(conn: psycopg.Connection) -> tuple[UUID, UUID]:
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
    return system_id, run_id


def _insert_activation(
    conn: psycopg.Connection,
    system_id: UUID,
    run_id: UUID,
    *,
    plan_identity: str = "sha256:" + "a" * 64,
    state: str = "preparing",
    cleanup_complete: bool = False,
) -> tuple[UUID, UUID]:
    activation_id, owner_id = uuid4(), uuid4()
    recovery_states = {"recovering", "recovered", "recovery_conflict", "recovery_failed"}
    attempt_id = uuid4() if state in recovery_states else None
    evidence_states = recovery_states | {"prepared", "activating", "active"}
    materialization = (
        {"schema": "external-boot-materialization-v1"} if state in evidence_states else None
    )
    recovery_point = (
        {"schema": "external-boot-recovery-v1"}
        if state in {"prepared", "activating", "active"}
        else None
    )
    pre_recovery = (
        {"schema": "external-boot-pre-recovery-evidence-v1"} if state in recovery_states else None
    )
    terminal = (
        {"schema": "external-boot-terminal-evidence-v1", "outcome": state}
        if state in {"active", "abandoned"}
        else None
    )
    teardown_states = {"recovery_conflict", "recovery_failed"}
    teardown = (
        {"schema": "external-boot-teardown-evidence-v1"}
        if cleanup_complete and state in teardown_states
        else None
    )
    cleanup = (
        {
            "schema": "external-boot-cleanup-evidence-v1",
            "mode": (
                "system_teardown"
                if state in {"recovery_conflict", "recovery_failed"}
                else "ordinary"
            ),
        }
        if cleanup_complete
        else None
    )
    conn.execute(
        "INSERT INTO external_boot_activations "
        "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
        "state, cleanup_complete, activation_readiness_deadline, materialization, "
        "recovery_point, pre_recovery_evidence, terminal_evidence, teardown_evidence, "
        "cleanup_evidence, current_attempt_id) VALUES "
        "(%s, %s, %s, %s, %s, 1, %s, %s, "
        "CASE WHEN %s IN ('activating', 'active') THEN now() ELSE NULL END, "
        "%s, %s, %s, %s, %s, %s, %s)",
        (
            activation_id,
            system_id,
            run_id,
            plan_identity,
            owner_id,
            state,
            cleanup_complete,
            state,
            Jsonb(materialization) if materialization is not None else None,
            Jsonb(recovery_point) if recovery_point is not None else None,
            Jsonb(pre_recovery) if pre_recovery is not None else None,
            Jsonb(terminal) if terminal is not None else None,
            Jsonb(teardown) if teardown is not None else None,
            Jsonb(cleanup) if cleanup is not None else None,
            attempt_id,
        ),
    )
    if attempt_id is not None:
        attempt_state = {
            "recovering": "recovering",
            "recovered": "recovered",
            "recovery_conflict": "conflict",
            "recovery_failed": "failed",
        }[state]
        conn.execute(
            "INSERT INTO external_boot_recovery_attempts "
            "(activation_id, attempt_number, attempt_id, authority_generation, recovery_basis, "
            "recovery_readiness_deadline, state, conflict_evidence, terminal_evidence) "
            "VALUES (%s, 1, %s, 1, 'pre_recovery', "
            "CASE WHEN %s = 'recovering' THEN now() ELSE NULL END, %s, %s, %s)",
            (
                activation_id,
                attempt_id,
                attempt_state,
                attempt_state,
                Jsonb({"schema": "external-boot-conflict-evidence-v1"})
                if attempt_state == "conflict"
                else None,
                Jsonb({"schema": "external-boot-terminal-evidence-v1"})
                if attempt_state in {"failed", "recovered"}
                else None,
            ),
        )
    return activation_id, owner_id


def test_migration_creates_four_ledgers_and_run_system_key(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'external_boot_%'"
            ).fetchall()
        }
        assert tables == {
            "external_boot_activations",
            "external_boot_reservations",
            "external_boot_reservation_releases",
            "external_boot_recovery_attempts",
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename IN ('runs', 'external_boot_activations')"
            ).fetchall()
        }
        assert "runs_id_system_id_key" in indexes
        assert "external_boot_activations_one_live_per_system" in indexes


def test_activation_run_system_binding_and_partial_uniqueness(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        _insert_activation(conn, system_id, run_id, plan_identity="sha256:" + "b" * 64)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_activation(conn, system_id, run_id)
        conn.rollback()

    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        other_system_id, _ = _seed_run(conn)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_activation(conn, other_system_id, run_id)


@pytest.mark.parametrize("state", ["recovered", "abandoned"])
def test_clean_normal_terminal_leaves_partial_uniqueness(migrated_url: str, state: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        _insert_activation(conn, system_id, run_id, state=state, cleanup_complete=True)
        _insert_activation(conn, system_id, run_id, plan_identity="sha256:" + "b" * 64)


@pytest.mark.parametrize("state", ["recovery_failed", "recovery_conflict"])
def test_clean_teardown_terminal_stays_in_partial_uniqueness(migrated_url: str, state: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        _insert_activation(conn, system_id, run_id, state=state, cleanup_complete=True)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_activation(conn, system_id, run_id)


def test_cleanup_matrix_rejects_non_cleanup_state(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        with pytest.raises(psycopg.errors.CheckViolation, match="cleanup_"):
            _insert_activation(conn, system_id, run_id, cleanup_complete=True)


@pytest.mark.parametrize(
    "state",
    [
        "prepared",
        "activating",
        "active",
        "recovering",
        "recovery_conflict",
        "recovery_failed",
        "recovered",
    ],
)
def test_state_matrix_rejects_missing_evidence(migrated_url: str, state: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        with pytest.raises(psycopg.errors.CheckViolation, match="state_evidence"):
            conn.execute(
                "INSERT INTO external_boot_activations "
                "(id, system_id, run_id, plan_identity, operation_owner_id, "
                "authority_generation, state, activation_readiness_deadline) "
                "VALUES (%s, %s, %s, %s, %s, 1, %s, "
                "CASE WHEN %s IN ('activating', 'active') THEN now() ELSE NULL END)",
                (uuid4(), system_id, run_id, "sha256:" + "e" * 64, uuid4(), state, state),
            )


def test_cleanup_requires_mode_evidence(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        with pytest.raises(psycopg.errors.CheckViolation, match="cleanup_evidence"):
            conn.execute(
                "INSERT INTO external_boot_activations "
                "(id, system_id, run_id, plan_identity, operation_owner_id, "
                "authority_generation, state, cleanup_complete, terminal_evidence) "
                "VALUES (%s, %s, %s, %s, %s, 1, 'abandoned', true, %s)",
                (
                    uuid4(),
                    system_id,
                    run_id,
                    "sha256:" + "e" * 64,
                    uuid4(),
                    Jsonb(
                        {
                            "schema": "external-boot-terminal-evidence-v1",
                            "outcome": "abandoned",
                        }
                    ),
                ),
            )


@pytest.mark.parametrize("state", ["active", "abandoned"])
def test_state_matrix_rejects_untyped_json_evidence(migrated_url: str, state: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        with pytest.raises(psycopg.errors.CheckViolation, match="evidence"):
            conn.execute(
                "INSERT INTO external_boot_activations "
                "(id, system_id, run_id, plan_identity, operation_owner_id, "
                "authority_generation, state, activation_readiness_deadline, materialization, "
                "recovery_point, terminal_evidence) VALUES "
                "(%s, %s, %s, %s, %s, 1, %s, "
                "CASE WHEN %s = 'active' THEN now() ELSE NULL END, %s, %s, '{}'::jsonb)",
                (
                    uuid4(),
                    system_id,
                    run_id,
                    "sha256:" + "e" * 64,
                    uuid4(),
                    state,
                    state,
                    Jsonb({"schema": "external-boot-materialization-v1"})
                    if state == "active"
                    else None,
                    Jsonb({"schema": "external-boot-recovery-v1"}) if state == "active" else None,
                ),
            )


def test_reservation_state_and_immutable_identities(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        activation_id, _ = _insert_activation(conn, system_id, run_id)
        conn.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id, store_identity, owner_key, reserved_bytes, state) "
            "VALUES (%s, 'stores/main', 'owners/a', 4096, 'pending')",
            (activation_id,),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="ready_at"):
            conn.execute(
                "UPDATE external_boot_reservations SET state = 'ready' WHERE activation_id = %s",
                (activation_id,),
            )
        conn.rollback()

    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        activation_id, _ = _insert_activation(conn, system_id, run_id)
        conn.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id, store_identity, owner_key, reserved_bytes, state) "
            "VALUES (%s, 'stores/main', 'owners/a', 4096, 'pending')",
            (activation_id,),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            conn.execute(
                "UPDATE external_boot_reservations SET reserved_bytes = 1 WHERE activation_id = %s",
                (activation_id,),
            )


def test_attempt_checks_and_release_tombstone_immutability(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        activation_id, _ = _insert_activation(conn, system_id, run_id)
        with pytest.raises(psycopg.errors.CheckViolation, match="attempt_deadline"):
            conn.execute(
                "INSERT INTO external_boot_recovery_attempts "
                "(activation_id, attempt_number, attempt_id, authority_generation, "
                "recovery_basis, state) VALUES (%s, 1, %s, 1, 'recovery_point', 'recovering')",
                (activation_id, uuid4()),
            )
        conn.rollback()

    with psycopg.connect(migrated_url) as conn:
        system_id, run_id = _seed_run(conn)
        activation_id, _ = _insert_activation(conn, system_id, run_id)
        conn.execute(
            "INSERT INTO external_boot_reservation_releases "
            "(activation_id, store_identity, owner_key, reserved_bytes, release_identity, "
            "release_evidence) VALUES (%s, 'stores/main', 'owners/a', 4096, %s, %s)",
            (
                activation_id,
                "sha256:" + "b" * 64,
                Jsonb({"schema": "external-boot-release-evidence-v1"}),
            ),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            conn.execute(
                "UPDATE external_boot_reservation_releases SET reserved_bytes = 1 "
                "WHERE activation_id = %s",
                (activation_id,),
            )


def test_runtime_roles_have_only_planned_privileges(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT has_table_privilege('kdive_server', 'external_boot_activations', 'UPDATE')"
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT has_table_privilege('kdive_worker', 'external_boot_activations', 'SELECT')"
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT has_table_privilege('kdive_worker', 'external_boot_activations', 'UPDATE')"
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT has_table_privilege('kdive_reconciler', "
            "'external_boot_reservation_releases', 'DELETE')"
        ).fetchone() == (False,)
