"""Migration 0102 persists cursors for every public build-GC lane (#1519)."""

from __future__ import annotations

import psycopg

from kdive.db import migrate


def test_0102_seeds_public_build_gc_lanes(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)

    rows = pg_conn.execute(
        "SELECT lane, after_id FROM build_artifact_gc_cursors ORDER BY lane"
    ).fetchall()

    assert rows == [
        ("closed-investigations", None),
        ("closed-legacy-artifacts", None),
        ("expired-legacy-artifacts", None),
    ]


def test_0102_precedes_worker_incarnation_migration() -> None:
    migrations = migrate.discover_migrations()

    assert [(migration.version, migration.filename) for migration in migrations[-8:]] == [
        ("0102", "0102_build_artifact_gc_cursors.sql"),
        ("0103", "0103_worker_incarnations.sql"),
        ("0104", "0104_worker_fence_roles.sql"),
        ("0105", "0105_worker_fence_functions.sql"),
        ("0106", "0106_worker_fence_protocol_claim.sql"),
        ("0107", "0107_process_role_data_access.sql"),
        ("0108", "0108_worker_fence_runtime_paths.sql"),
        ("0109", "0109_kubernetes_credential_envelopes.sql"),
    ]
