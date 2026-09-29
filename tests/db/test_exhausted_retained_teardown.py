"""Real-Postgres proofs that an exhausted retained System teardown is not stranded (#2917).

ADR-0620's #2917 amendment: a `retained_quarantine` receipt on the job's final attempt ends the
job `failed` and its teardown authority `retired`, so the public teardown's `failed` recycle can
re-run it; migration 0164 also moves jobs already stranded `queued` and exhausted.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, TeardownPayload
from tests.db.external_boot_authority_support import (
    _allocate,
    _Allocated,
    _AuthorityCase,
    _RoleDsns,
    _seed_case,
)
from tests.db.external_boot_journal_support import (
    _finalize,
    _make_current,
    _proof,
    _ready_teardown_case,
)

_AUTHORIZING = Authorizing(principal="p", agent_session=None, project="proj")
_DIGEST = "sha256:" + "e" * 64


def test_migration_0164_is_registered() -> None:
    assert "0164" in [item.version for item in migrate.discover_migrations()]


def _retain(
    migrated_url: str, role_dsns: _RoleDsns, case: _AuthorityCase, *, final: bool
) -> _Allocated:
    """Commit a `retained_quarantine` receipt for the job's current attempt."""
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    proof = _proof(case, "retained_quarantine")
    with psycopg.connect(migrated_url) as conn:
        digest = _make_current(conn, case, authority, proof, 2, category="conflict")
        if final:
            conn.execute(
                "UPDATE jobs SET max_attempts = attempt, "
                "authorizing = authorizing || '{\"agent_session\": null}' WHERE id = %s",
                (case.job_id,),
            )
    assert _finalize(role_dsns, case, authority, proof, 2, digest) == "retained"
    return authority


def _job(url: str, job_id: UUID) -> tuple[Any, ...]:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT state, error_category, attempt, max_attempts FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    assert row is not None
    return row


def _authority(url: str, authority_id: UUID) -> tuple[Any, ...]:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT state, superseded_at IS NULL, retired_at IS NOT NULL "
            "FROM external_boot_authorities WHERE id = %s",
            (authority_id,),
        ).fetchone()
    assert row is not None
    return row


def _routes(url: str, system_id: UUID) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT activation_id FROM resolve_external_boot_system_teardown_dispatch_binding(%s)",
            (system_id,),
        ).fetchall()


def test_final_attempt_retained_teardown_is_dead_lettered(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _ready_teardown_case(migrated_url, "f")
    # The seeded activation has no other authority, so the teardown's own row is its only route.
    assert _routes(migrated_url, case.system_id) == []

    authority = _retain(migrated_url, authority_role_dsns, case, final=True)

    assert _job(migrated_url, case.job_id) == ("failed", "conflict", 1, 1)
    with psycopg.connect(migrated_url) as conn:
        lease = conn.execute(
            "SELECT worker_id, lease_expires_at, heartbeat_at, failure_context FROM jobs "
            "WHERE id = %s",
            (case.job_id,),
        ).fetchone()
    assert lease == (None, None, None, {})
    assert _authority(migrated_url, authority.authority_id) == ("retired", True, True)
    assert _routes(migrated_url, case.system_id) == [(case.activation_id,)]


def test_non_final_retained_teardown_still_requeues(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _ready_teardown_case(migrated_url, "n")

    authority = _retain(migrated_url, authority_role_dsns, case, final=False)

    assert _job(migrated_url, case.job_id)[0] == "queued"
    assert _authority(migrated_url, authority.authority_id)[0] == "superseded"


def _release_count(url: str, case: _AuthorityCase) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM external_boot_reservation_releases WHERE activation_id = %s",
            (case.activation_id,),
        ).fetchone()
    assert row is not None
    return row[0]


async def _recycle(url: str, case: _AuthorityCase) -> Any:
    with psycopg.connect(url) as conn:
        marker = conn.execute(
            "SELECT payload -> 'external_boot_authority_v1' FROM jobs WHERE id = %s",
            (case.job_id,),
        ).fetchone()
    assert marker is not None
    async with await psycopg.AsyncConnection.connect(url) as conn:
        return await queue.enqueue(
            conn,
            JobKind.TEARDOWN,
            TeardownPayload.model_validate(
                {"system_id": str(case.system_id), "external_boot_authority_v1": marker[0]}
            ),
            _AUTHORIZING,
            f"external-authority-{case.job_id}",
            recycle=queue.JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED,
        )


@pytest.mark.anyio
async def test_dead_lettered_retained_teardown_recycles_and_credits_once(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _ready_teardown_case(migrated_url, "r")
    old = _retain(migrated_url, authority_role_dsns, case, final=True)

    recycled = await _recycle(migrated_url, case)
    assert (recycled.id, recycled.state.value, recycled.attempt) == (case.job_id, "queued", 0)

    # A different incarnation claims the recycled job, as claim_worker_job would.
    successor = replace(
        case, worker_id=f"docker:external-authority-r2-{uuid4()}", credential=b"s" * 32
    )
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
            "VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
            (successor.worker_id, successor.credential),
        )
        conn.execute(
            "UPDATE jobs SET state = 'running', attempt = 1, worker_id = %s, "
            "lease_expires_at = now() + interval '5 minutes', heartbeat_at = now() WHERE id = %s",
            (successor.worker_id, case.job_id),
        )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        new = _allocate(worker, successor)
    assert new.authority_id != old.authority_id
    proof = _proof(case, "complete_ready")
    with psycopg.connect(migrated_url) as conn:
        digest = _make_current(conn, successor, new, proof, 4)
    assert _finalize(authority_role_dsns, successor, new, proof, 4, digest) == "applied"
    assert _finalize(authority_role_dsns, successor, new, proof, 4, digest) == "applied"
    assert _release_count(migrated_url, case) == 1

    # A teardown retried after success replays the succeeded job and credits nothing more.
    replayed = await _recycle(migrated_url, case)
    assert (replayed.id, replayed.state.value) == (case.job_id, "succeeded")
    assert _release_count(migrated_url, case) == 1


def _apply_through(conn: psycopg.Connection, last_version: str) -> None:
    """Apply and record migrations up to last_version, as the runner would."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, "
        "filename text NOT NULL, checksum text NOT NULL, "
        "applied_at timestamptz NOT NULL DEFAULT now())"
    )
    for m in migrate.discover_migrations():
        if m.version > last_version:
            break
        conn.execute(m.sql.encode())
        conn.execute(
            "INSERT INTO schema_migrations (version, filename, checksum) VALUES (%s, %s, %s)",
            (m.version, m.filename, m.checksum),
        )


def _stranded_before_0164(conn: psycopg.Connection) -> tuple[_AuthorityCase, UUID]:
    """A job left `queued` at its final attempt by a pre-0164 retained receipt."""
    case = _seed_case(conn, purpose="teardown", worker_suffix="m")
    authority_id = uuid4()
    conn.execute(
        "INSERT INTO external_boot_authorities "
        "(id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id, "
        "job_attempt, purpose, provider_kind, authority_instance, worker_incarnation, operation, "
        "operation_identity, operation_digest, generation, state, acknowledged_at, superseded_at) "
        "SELECT %s, system_id, %s, id, run_id, plan_identity, %s, 3, 'teardown', "
        "'local-libvirt', %s, %s, 'teardown', %s, %s, 1, 'superseded', now(), now() "
        "FROM external_boot_activations WHERE id = %s",
        (
            authority_id,
            case.allocation_id,
            case.job_id,
            case.authority_instance,
            case.worker_id,
            case.operation_identity,
            _DIGEST,
            case.activation_id,
        ),
    )
    conn.execute(
        "INSERT INTO external_boot_teardown_receipts (root_authority_id, job_id, job_attempt, "
        "journal_sequence, journal_digest, proof_bytes, proof_digest, disposition, consumed) "
        "VALUES (%s, %s, 3, 2, %s, '\\x7b7d', %s, 'retained_quarantine', false)",
        (authority_id, case.job_id, _DIGEST, _DIGEST),
    )
    conn.execute(
        "UPDATE jobs SET state = 'queued', attempt = 3, max_attempts = 3, worker_id = NULL, "
        "lease_expires_at = NULL, heartbeat_at = NULL, error_category = 'conflict' "
        "WHERE id = %s",
        (case.job_id,),
    )
    return case, authority_id


def _plain_teardown(conn: psycopg.Connection, *, attempt: int, marked: bool) -> UUID:
    job_id = uuid4()
    payload = {"system_id": str(uuid4())}
    if marked:
        payload["external_boot_authority_v1"] = {"system_id": payload["system_id"]}
    conn.execute(
        "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, authorizing, "
        "dedup_key) VALUES (%s, 'teardown', %s, 'queued', %s, 3, %s, %s)",
        (job_id, Jsonb(payload), attempt, Jsonb({"principal": "p", "project": "proj"}), job_id),
    )
    return job_id


def test_migration_0164_dead_letters_stranded_retained_teardown(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0163")
    with pg_conn.transaction():  # the seed's activation foreign keys are deferred
        case, authority_id = _stranded_before_0164(pg_conn)
        retrying = _plain_teardown(pg_conn, attempt=1, marked=True)
        ordinary = _plain_teardown(pg_conn, attempt=3, marked=False)

    assert "0164" in migrate.apply_migrations(pg_conn)

    row = pg_conn.execute(
        "SELECT j.state, j.error_category, a.state, a.superseded_at IS NULL, "
        "a.retired_at IS NOT NULL FROM jobs AS j, external_boot_authorities AS a "
        "WHERE j.id = %s AND a.id = %s",
        (case.job_id, authority_id),
    ).fetchone()
    assert row == ("failed", "conflict", "retired", True, True)
    routes = pg_conn.execute(
        "SELECT activation_id FROM resolve_external_boot_system_teardown_dispatch_binding(%s)",
        (case.system_id,),
    ).fetchall()
    assert routes == [(case.activation_id,)]
    states = pg_conn.execute(
        "SELECT state FROM jobs WHERE id = ANY(%s) ORDER BY attempt", ([retrying, ordinary],)
    ).fetchall()
    assert states == [("queued",), ("queued",)]
