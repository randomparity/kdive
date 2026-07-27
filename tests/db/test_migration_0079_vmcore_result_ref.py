"""Migration 0079 backfills capture_vmcore result_ref to the redacted artifact id (#1591)."""

from __future__ import annotations

import uuid

import psycopg
from psycopg.types.json import Jsonb

from kdive.db import migrate


def _apply_through(conn: psycopg.Connection, last_version: str) -> None:
    """Apply migrations up to and including ``last_version`` without the migration runner."""
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


def _apply_0079(conn: psycopg.Connection) -> None:
    migration = next(m for m in migrate.discover_migrations() if m.version == "0079")
    conn.execute(migration.sql.encode())


def _insert_job(conn: psycopg.Connection, kind: str, dedup_key: str, result_ref: str | None) -> str:
    row = conn.execute(
        "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key, "
        "result_ref) VALUES (%s, %s, 'succeeded', 3, %s, %s, %s) RETURNING id",
        (
            kind,
            Jsonb({}),
            Jsonb({"principal": "worker", "agent_session": None, "project": "proj"}),
            dedup_key,
            result_ref,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_artifact(
    conn: psycopg.Connection, owner_id: str, object_key: str, sensitivity: str
) -> str:
    row = conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES ('runs', %s, %s, 'e', %s, 'vmcore') RETURNING id",
        (owner_id, object_key, sensitivity),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _result_ref(conn: psycopg.Connection, job_id: str) -> str | None:
    row = conn.execute("SELECT result_ref FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row is not None
    return None if row[0] is None else str(row[0])


def test_0079_backfills_raw_key_to_the_redacted_artifact_id(pg_conn: psycopg.Connection) -> None:
    """A historical raw-key result resolves to its redacted sibling's artifact id."""
    _apply_through(pg_conn, "0078")
    run_id = str(uuid.uuid4())
    raw = f"local/runs/{run_id}/vmcore-kdump"
    _insert_artifact(pg_conn, run_id, raw, "sensitive")
    redacted_id = _insert_artifact(pg_conn, run_id, f"{raw}-redacted", "redacted")
    job_id = _insert_job(pg_conn, "capture_vmcore", "job-mapped", raw)

    _apply_0079(pg_conn)

    assert _result_ref(pg_conn, job_id) == redacted_id


def test_0079_nulls_the_ref_when_the_redacted_row_is_absent(pg_conn: psycopg.Connection) -> None:
    """The documented fallback: no redacted sibling -> NULL, never a raw key a viewer can't read.

    The raw core can outlive its redacted derivative (artifact reclaim/expiry). ADR-0466 makes
    ``refs.result`` mean exactly one thing, so an unmappable row publishes no reference at all.
    """
    _apply_through(pg_conn, "0078")
    run_id = str(uuid.uuid4())
    raw = f"local/runs/{run_id}/vmcore-host_dump"
    _insert_artifact(pg_conn, run_id, raw, "sensitive")  # raw survives, redacted does not
    job_id = _insert_job(pg_conn, "capture_vmcore", "job-unmapped", raw)

    _apply_0079(pg_conn)

    assert _result_ref(pg_conn, job_id) is None


def test_0079_leaves_other_kinds_and_null_refs_alone(pg_conn: psycopg.Connection) -> None:
    """Only capture_vmcore rows holding a raw-vmcore-shaped key are touched."""
    _apply_through(pg_conn, "0078")
    run_id = str(uuid.uuid4())
    raw = f"local/runs/{run_id}/vmcore-kdump"
    _insert_artifact(pg_conn, run_id, raw, "sensitive")
    _insert_artifact(pg_conn, run_id, f"{raw}-redacted", "redacted")
    other_kind = _insert_job(pg_conn, "boot", "job-boot", raw)
    no_ref = _insert_job(pg_conn, "capture_vmcore", "job-no-ref", None)
    unrelated = _insert_job(pg_conn, "capture_vmcore", "job-other-key", "local/runs/x/console")

    _apply_0079(pg_conn)

    assert _result_ref(pg_conn, other_kind) == raw
    assert _result_ref(pg_conn, no_ref) is None
    assert _result_ref(pg_conn, unrelated) == "local/runs/x/console"


def test_0079_is_a_no_op_on_already_migrated_rows(pg_conn: psycopg.Connection) -> None:
    """Re-running must not clobber an artifact id: it carries no ``/vmcore-`` to match."""
    _apply_through(pg_conn, "0078")
    run_id = str(uuid.uuid4())
    raw = f"local/runs/{run_id}/vmcore-kdump"
    _insert_artifact(pg_conn, run_id, raw, "sensitive")
    redacted_id = _insert_artifact(pg_conn, run_id, f"{raw}-redacted", "redacted")
    job_id = _insert_job(pg_conn, "capture_vmcore", "job-rerun", raw)

    _apply_0079(pg_conn)
    _apply_0079(pg_conn)

    assert _result_ref(pg_conn, job_id) == redacted_id
