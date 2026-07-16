"""Migration 0069 widens jobs.kind CHECK to admit 'watch_for_crash' (#984)."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from kdive.domain.operations.jobs import JobKind


def _apply_through(conn: psycopg.Connection, last_version: str) -> None:
    """Apply migrations up to and including last_version without the migration runner."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    text PRIMARY KEY,
            filename   text NOT NULL,
            checksum   text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    for m in migrate.discover_migrations():
        if m.version > last_version:
            break
        conn.execute(m.sql.encode())
        conn.execute(
            "INSERT INTO schema_migrations (version, filename, checksum) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (m.version, m.filename, m.checksum),
        )


def _insert_job(conn: psycopg.Connection, kind: str, dedup_key: str) -> None:
    conn.execute(
        "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
        "VALUES (%s, %s, 'queued', 3, %s, %s)",
        (
            kind,
            Jsonb({}),
            Jsonb({"principal": "worker", "agent_session": None, "project": "proj"}),
            dedup_key,
        ),
    )


def test_watch_for_crash_enum_value() -> None:
    """JobKind.WATCH_FOR_CRASH must carry the exact string the SQL CHECK admits."""
    assert JobKind.WATCH_FOR_CRASH.value == "watch_for_crash"


def test_pre_migration_0069_rejects_watch_for_crash(pg_conn: psycopg.Connection) -> None:
    """Before 0069 lands, 'watch_for_crash' violates jobs_kind_check."""
    _apply_through(pg_conn, "0068")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_job(pg_conn, "watch_for_crash", "pre-0069-test")


def test_migration_0069_admits_watch_for_crash(pg_conn: psycopg.Connection) -> None:
    """After all migrations, inserting kind='watch_for_crash' succeeds."""
    migrate.apply_migrations(pg_conn)
    _insert_job(pg_conn, "watch_for_crash", "post-0069-test")
    row = pg_conn.execute("SELECT kind FROM jobs WHERE dedup_key = 'post-0069-test'").fetchone()
    assert row is not None and row[0] == "watch_for_crash"


def test_migration_0069_keeps_all_prior_kinds(pg_conn: psycopg.Connection) -> None:
    """0069 must not drop any existing kind from the jobs_kind_check constraint."""
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'jobs_kind_check'"
    ).fetchone()
    assert row is not None
    definition = row[0]
    prior_kinds = [
        "provision",
        "reprovision",
        "teardown",
        "build",
        "install",
        "boot",
        "force_crash",
        "power",
        "capture_vmcore",
        "image_build",
        "diagnostics_worker_check",
        "build_install_boot",
        "authorize_ssh_key",
        "console_rotate",
        "diagnostic_sysrq",
        "check_ssh_reachable",
        "watch_for_crash",
    ]
    missing = [k for k in prior_kinds if f"'{k}'" not in definition]
    assert not missing, f"jobs_kind_check is missing kinds: {missing}"
