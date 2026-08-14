"""Historical protocol-3 cutover and active capture protocol boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from kdive.db import migrate


def _migration(version: str) -> migrate.Migration:
    return next(item for item in migrate.discover_migrations() if item.version == version)


def _apply_through(conn: psycopg.Connection, version: str) -> None:
    for migration in migrate.discover_migrations():
        conn.execute(migration.sql.encode())
        if migration.version == version:
            return


@pytest.fixture
def pre_cutover(pg_conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    _apply_through(pg_conn, "0111")
    yield pg_conn


def _insert_incarnation(
    conn: psycopg.Connection,
    name: str,
    *,
    state: str,
    protocol: int = 2,
) -> None:
    terminated = state == "terminated"
    conn.execute(
        "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
        "credential_hash, fence_protocol, state, terminated_at, outcome) VALUES "
        "(%s, 'docker', %s, %s, %s, %s, CASE WHEN %s THEN clock_timestamp() END, "
        "CASE WHEN %s THEN 'killed' END)",
        (
            name,
            Jsonb(
                {
                    "container_id": hashlib.sha256(name.encode()).hexdigest(),
                    "project": "kdive",
                    "service": "worker",
                    "ordinal": "0",
                }
            ),
            hashlib.sha256(f"credential:{name}".encode()).digest(),
            protocol,
            state,
            terminated,
            terminated,
        ),
    )


def test_cutover_rejects_any_unterminated_legacy_incarnation_without_schema_mutation(
    pre_cutover: psycopg.Connection,
) -> None:
    _insert_incarnation(pre_cutover, "docker:active", state="active")
    with pytest.raises(errors.CheckViolation, match="offline capture protocol cutover blocked"):
        pre_cutover.execute(_migration("0112").sql.encode())
    assert pre_cutover.execute("SELECT to_regclass('public.capture_operations')").fetchone() == (
        None,
    )


def test_cutover_cancels_residual_running_capture_and_preserves_queued_job(
    pre_cutover: psycopg.Connection,
) -> None:
    owner = "docker:stopped"
    _insert_incarnation(pre_cutover, owner, state="terminated")
    running_id, queued_id = uuid4(), uuid4()
    pre_cutover.execute(
        "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, heartbeat_at, error_category, failure_context, authorizing, "
        "dedup_key) VALUES "
        "(%s, 'capture_traffic', 'running', 2, 3, %s, now(), now(), "
        "'infrastructure_failure', '{\"old\":\"value\"}'::jsonb, '{}'::jsonb, %s), "
        "(%s, 'capture_traffic', 'queued', 0, 3, NULL, NULL, NULL, NULL, '{}'::jsonb, "
        "'{}'::jsonb, %s)",
        (running_id, owner, f"running-{running_id}", queued_id, f"queued-{queued_id}"),
    )
    pre_cutover.execute(_migration("0112").sql.encode())

    running = pre_cutover.execute(
        "SELECT state, attempt, worker_id, lease_expires_at, heartbeat_at, error_category, "
        "failure_context FROM jobs WHERE id = %s",
        (running_id,),
    ).fetchone()
    assert running == (
        "canceled",
        2,
        None,
        None,
        None,
        None,
        {"reason": "offline_capture_protocol_cutover"},
    )
    queued = pre_cutover.execute(
        "SELECT state, attempt, worker_id FROM jobs WHERE id = %s", (queued_id,)
    ).fetchone()
    assert queued == ("queued", 0, None)


def test_registration_uses_the_same_global_cutover_lock_and_rejects_protocol_2(
    pre_cutover: psycopg.Connection,
    postgres_url: str,
) -> None:
    pre_cutover.execute(_migration("0112").sql.encode())
    with (
        psycopg.connect(postgres_url) as blocker,
        psycopg.connect(postgres_url) as contender,
    ):
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('kdive:capture-protocol', 1951))"
        )
        contender.execute("SET LOCAL lock_timeout = '100ms'")
        contender.execute("SET LOCAL ROLE kdive_lifecycle_witness")
        with pytest.raises(errors.LockNotAvailable):
            contender.execute(
                "SELECT public.register_worker_incarnation(%s, 'docker', %s, %s, 3)",
                (
                    "docker:blocked",
                    Jsonb({"container_id": "a" * 64}),
                    hashlib.sha256(b"blocked").digest(),
                ),
            )

    pre_cutover.execute("SET SESSION AUTHORIZATION kdive_lifecycle_witness")
    try:
        with pytest.raises(errors.InvalidParameterValue, match="protocol 3 is required"):
            pre_cutover.execute(
                "SELECT public.register_worker_incarnation(%s, 'docker', %s, %s, 2)",
                (
                    "docker:stale",
                    Jsonb({"container_id": "b" * 64}),
                    hashlib.sha256(b"stale").digest(),
                ),
            )
    finally:
        pre_cutover.execute("RESET SESSION AUTHORIZATION")


def test_fresh_install_records_complete_protocol_4_cutoff(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    columns = {
        row[0]
        for row in pg_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'capture_operation_cutoff'"
        ).fetchall()
    }
    assert columns == {
        "singleton",
        "protocol",
        "operation_quiescent",
        "publication_closed",
        "complete",
        "cutoff_at",
    }
    assert pg_conn.execute(
        "SELECT singleton, protocol, operation_quiescent, publication_closed, complete, "
        "cutoff_at <= clock_timestamp() "
        "FROM capture_operation_cutoff"
    ).fetchone() == (True, 4, True, True, True, True)


def test_fresh_install_authentication_and_claim_require_protocol_4(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    protocol = pg_conn.execute(
        "SELECT protocol FROM capture_operation_cutoff WHERE singleton"
    ).fetchone()
    assert protocol == (4,)
    function = pg_conn.execute(
        "SELECT pg_get_functiondef("
        "'public.claim_worker_job(text,bytea,interval,text[])'::regprocedure)"
    ).fetchone()
    assert function is not None
    assert "fence_protocol = 4" in function[0]
