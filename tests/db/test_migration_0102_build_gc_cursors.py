"""Migration 0102 persists cursors for every public build-GC lane (#1519)."""

from __future__ import annotations

import psycopg

from kdive.db import migrate


def test_0102_is_retained_in_migration_history(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)

    assert pg_conn.execute(
        "SELECT version FROM schema_migrations WHERE version = '0102'"
    ).fetchone() == ("0102",)


def test_0102_precedes_worker_incarnation_migration() -> None:
    migrations = migrate.discover_migrations()

    assert [(migration.version, migration.filename) for migration in migrations[-21:]] == [
        ("0103", "0103_worker_incarnations.sql"),
        ("0104", "0104_worker_fence_roles.sql"),
        ("0105", "0105_worker_fence_functions.sql"),
        ("0106", "0106_worker_fence_protocol_claim.sql"),
        ("0107", "0107_process_role_data_access.sql"),
        ("0108", "0108_worker_fence_runtime_paths.sql"),
        ("0109", "0109_kubernetes_credential_envelopes.sql"),
        ("0110", "0110_idempotent_worker_termination.sql"),
        ("0111", "0111_restrict_pinned_job_deletion.sql"),
        ("0112", "0112_capture_operation_supervision.sql"),
        ("0113", "0113_capture_publication_fence.sql"),
        ("0114", "0114_host_dump_volume_leases.sql"),
        ("0115", "0115_capture_reap_state.sql"),
        ("0116", "0116_capture_claimable_queue_depth.sql"),
        ("0117", "0117_worker_bootstrap_key_insert.sql"),
        ("0118", "0118_worker_audit_log_insert.sql"),
        ("0119", "0119_drop_obsolete_build_gc_cursors.sql"),
        ("0120", "0120_system_root_provenance.sql"),
        ("0121", "0121_external_boot_activations.sql"),
        ("0122", "0122_external_boot_authority.sql"),
        ("0123", "0123_external_boot_authority_journal.sql"),
    ]
