"""Build-new-only installation of capture publication protocol 4."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.types.json import Jsonb

from kdive.db import migrate


def _migration(version: str) -> migrate.Migration:
    return next(item for item in migrate.discover_migrations() if item.version == version)


def _apply_through(conn: psycopg.Connection, version: str) -> None:
    for migration in migrate.discover_migrations():
        conn.execute(migration.sql.encode())
        if migration.version == version:
            return


def _seed_worker(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
        "fence_protocol, credential_hash) VALUES "
        "('docker:seed', 'docker', %s, 3, %s)",
        (
            Jsonb(
                {
                    "container_id": "a" * 64,
                    "project": "kdive",
                    "service": "worker",
                    "ordinal": "0",
                }
            ),
            hashlib.sha256(b"seed-worker").digest(),
        ),
    )


def _seed_job(conn: psycopg.Connection) -> None:
    job_id = uuid4()
    conn.execute(
        "INSERT INTO jobs (id, kind, state, max_attempts, authorizing, dedup_key) "
        "VALUES (%s, 'capture_traffic', 'queued', 3, '{}'::jsonb, %s)",
        (job_id, f"seed-{job_id}"),
    )


def _seed_capture_operation(conn: psycopg.Connection) -> None:
    """Seed only this relevant table; replica mode bypasses its FK trigger dependencies."""
    conn.execute("SET session_replication_role = replica")
    try:
        conn.execute(
            "INSERT INTO capture_operations (job_id, job_attempt, worker_incarnation, "
            "provider_kind, resource_id, system_id, domain_name, request_digest, launch_token, "
            "host_instance) VALUES (%s, 1, 'local:seed', 'local-libvirt', %s, %s, 'guest', "
            "%s, %s, 'host-a')",
            (uuid4(), uuid4(), uuid4(), "a" * 64, "b" * 64),
        )
    finally:
        conn.execute("SET session_replication_role = origin")


def _seed_artifact(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES ('runs', %s, %s, 'etag', 'sensitive', 'pcap')",
        (uuid4(), f"captures/{uuid4()}"),
    )


_SEEDS: dict[str, Callable[[psycopg.Connection], None]] = {
    "worker_incarnations": _seed_worker,
    "jobs": _seed_job,
    "capture_operations": _seed_capture_operation,
    "artifacts": _seed_artifact,
}


def _rows(conn: psycopg.Connection, table: str) -> list[object]:
    return [
        row[0]
        for row in conn.execute(
            sql.SQL("SELECT to_jsonb(subject) FROM {} AS subject ORDER BY subject.id").format(
                sql.Identifier(table)
            )
            if table != "worker_incarnations"
            else sql.SQL(
                "SELECT to_jsonb(subject) FROM {} AS subject ORDER BY subject.incarnation"
            ).format(sql.Identifier(table))
        ).fetchall()
    ]


@pytest.mark.parametrize("table", sorted(_SEEDS))
def test_protocol_4_refuses_each_nonempty_population_without_mutation(
    pg_conn: psycopg.Connection,
    table: str,
) -> None:
    _apply_through(pg_conn, "0112")
    _SEEDS[table](pg_conn)
    before = {name: _rows(pg_conn, name) for name in _SEEDS}
    cutoff_before = pg_conn.execute(
        "SELECT to_jsonb(cutoff) FROM capture_operation_cutoff AS cutoff"
    ).fetchone()

    with pytest.raises(
        errors.CheckViolation,
        match="capture publication protocol 4 requires a fresh database",
    ):
        pg_conn.execute(_migration("0113").sql.encode())

    assert {name: _rows(pg_conn, name) for name in _SEEDS} == before
    assert (
        pg_conn.execute(
            "SELECT to_jsonb(cutoff) FROM capture_operation_cutoff AS cutoff"
        ).fetchone()
        == cutoff_before
    )
    assert (
        pg_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'capture_operations' "
            "AND column_name = 'publication_state'"
        ).fetchone()
        is None
    )


def test_empty_install_activates_complete_protocol_4_cutoff(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0112")
    old_cutoff = pg_conn.execute(
        "SELECT cutoff_at FROM capture_operation_cutoff WHERE singleton"
    ).fetchone()
    before = pg_conn.execute("SELECT clock_timestamp()").fetchone()
    assert old_cutoff is not None and before is not None

    pg_conn.execute(_migration("0113").sql.encode())

    after = pg_conn.execute("SELECT clock_timestamp()").fetchone()
    cutoff = pg_conn.execute(
        "SELECT protocol, operation_quiescent, publication_closed, complete, cutoff_at "
        "FROM capture_operation_cutoff WHERE singleton"
    ).fetchone()
    assert after is not None and cutoff is not None
    assert cutoff[:4] == (4, True, True, True)
    cutoff_at = cutoff[4]
    assert isinstance(cutoff_at, datetime)
    assert old_cutoff[0] < cutoff_at
    assert before[0] <= cutoff_at <= after[0]

    columns = {
        row[0]
        for row in pg_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'capture_operations'"
        ).fetchall()
    }
    assert {
        "publication_state",
        "publication_object_key",
        "publication_etag",
        "publication_artifact_id",
        "cleanup_capture_version_id",
        "publication_tombstone_version",
        "publication_started_at",
        "publication_closed_at",
        "spool_disposed_at",
    } <= columns
    stale_functions = pg_conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc AS p "
        "JOIN pg_namespace AS n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.prosecdef AND ("
        "pg_get_functiondef(p.oid) LIKE '%fence_protocol = 3%' OR "
        "pg_get_functiondef(p.oid) LIKE '%fence_protocol < 3%' OR "
        "pg_get_functiondef(p.oid) LIKE '%fence_protocol IS DISTINCT FROM 3%' OR "
        "pg_get_functiondef(p.oid) LIKE '%protocol 3%')"
    ).fetchall()
    assert stale_functions == []
