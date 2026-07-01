"""Migration 0055 widens jobs.kind CHECK to admit 'diagnostic_sysrq' (#925)."""

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


def test_diagnostic_sysrq_enum_value() -> None:
    """JobKind.DIAGNOSTIC_SYSRQ must carry the exact string the SQL CHECK admits."""
    assert JobKind.DIAGNOSTIC_SYSRQ.value == "diagnostic_sysrq"


def test_pre_migration_0055_rejects_diagnostic_sysrq(pg_conn: psycopg.Connection) -> None:
    """Before 0055 lands, 'diagnostic_sysrq' violates jobs_kind_check."""
    _apply_through(pg_conn, "0054")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_job(pg_conn, "diagnostic_sysrq", "pre-0055-test")


def test_migration_0055_admits_diagnostic_sysrq(pg_conn: psycopg.Connection) -> None:
    """After all migrations, inserting kind='diagnostic_sysrq' succeeds."""
    migrate.apply_migrations(pg_conn)
    _insert_job(pg_conn, "diagnostic_sysrq", "post-0055-test")
    row = pg_conn.execute("SELECT kind FROM jobs WHERE dedup_key = 'post-0055-test'").fetchone()
    assert row is not None and row[0] == "diagnostic_sysrq"


def test_migration_0055_keeps_all_prior_kinds(pg_conn: psycopg.Connection) -> None:
    """0055 must not drop any existing kind from the jobs_kind_check constraint."""
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
    ]
    missing = [k for k in prior_kinds if f"'{k}'" not in definition]
    assert not missing, f"jobs_kind_check is missing kinds: {missing}"
