"""Migration 0138 installs bounded recovery-object quarantine state."""

from __future__ import annotations

import psycopg

from kdive.db import migrate


def test_0138_creates_quarantine_request_tables_and_job_kind(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    tables = {
        row[0]
        for row in pg_conn.execute(
            "SELECT relname FROM pg_class WHERE relname IN "
            "('external_boot_recovery_quarantine', 'external_boot_recovery_orphan_requests')"
        ).fetchall()
    }
    assert tables == {
        "external_boot_recovery_quarantine",
        "external_boot_recovery_orphan_requests",
    }
    definition = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'jobs_kind_check'"
    ).fetchone()
    assert definition is not None
    assert "resolve_recovery_orphan" in definition[0]
