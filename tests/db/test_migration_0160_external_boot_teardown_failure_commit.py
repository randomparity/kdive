"""Real-Postgres proofs that a teardown failure commits for a System in its real state (#2881)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from kdive.domain.operations.jobs import Job
from kdive.jobs import queue
from tests.db.external_boot_authority_support import (
    _COMMIT_SIGNATURE,
    _JOURNAL,
    _PLAN,
    _QUIESCENCE,
    _allocate,
    _Allocated,
    _apply_through,
    _AuthorityCase,
    _RoleDsns,
    _seed_case,
    _set_real_state,
)

_FAILURE = {
    "schema": "external-boot-authority-result-v1",
    "operation": "fail",
    "error_category": "infrastructure_failure",
    "failure_context": {"phase": "provider-call"},
    "terminal": False,
}


def _migration_sql() -> str:
    return next(m for m in migrate.discover_migrations() if m.version == "0160").sql


def _acknowledge(conn: psycopg.Connection, case: _AuthorityCase, authority: _Allocated) -> None:
    conn.execute(
        "UPDATE external_boot_authorities SET state = 'current', acknowledged_at = now() "
        "WHERE id = %s",
        (authority.authority_id,),
    )
    conn.execute(
        "INSERT INTO external_boot_authority_acknowledgements "
        "(authority_id, system_id, generation, authority_instance, operation_identity, "
        "operation_digest, journal_sequence, journal_digest, positive_quiescence_digest) "
        "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)",
        (
            authority.authority_id,
            case.system_id,
            authority.generation,
            case.authority_instance,
            case.operation_identity,
            authority.operation_digest,
            _JOURNAL,
            _QUIESCENCE,
        ),
    )


def _commit(
    worker: psycopg.Connection,
    case: _AuthorityCase,
    authority: _Allocated,
    *,
    attempt: int = 1,
    terminal: bool = False,
) -> tuple[str, str | None]:
    row = worker.execute(
        "SELECT status, job_state FROM commit_external_boot_authority_result("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            case.credential,
            case.job_id,
            attempt,
            authority.authority_id,
            authority.generation,
            case.activation_id,
            case.run_id,
            case.system_id,
            _PLAN,
            case.purpose,
            case.provider_kind,
            case.authority_instance,
            case.operation_identity,
            authority.operation_digest,
            1,
            _JOURNAL,
            "teardown",
            Jsonb({**_FAILURE, "terminal": terminal}),
        ),
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _admitted(
    migrated_url: str, role_dsns: _RoleDsns, activation: str, system_state: str = "ready"
) -> tuple[_AuthorityCase, _Allocated]:
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown", worker_suffix="t")
        _set_real_state(seed, case, activation)
        seed.execute("UPDATE systems SET state = %s WHERE id = %s", (system_state, case.system_id))
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(migrated_url) as seed:
        _acknowledge(seed, case, authority)
    return case, authority


def _credit(conn: psycopg.Connection, case: _AuthorityCase) -> tuple[object, ...]:
    reservation = conn.execute(
        "SELECT state, reserved_bytes FROM external_boot_reservations WHERE activation_id = %s",
        (case.activation_id,),
    ).fetchone()
    releases = conn.execute(
        "SELECT count(*) FROM external_boot_reservation_releases WHERE activation_id = %s",
        (case.activation_id,),
    ).fetchone()
    return (reservation, releases)


def test_0160_patch_targets_exist_once(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0159")
    definition = pg_conn.execute(
        "SELECT pg_get_functiondef(%s::regprocedure)", (f"public.{_COMMIT_SIGNATURE}",)
    ).fetchone()
    assert definition is not None
    targets = re.findall(r"v_old_\w+ constant text := \$old\$(.*?)\$old\$;", _migration_sql(), re.S)
    assert len(targets) == 2
    for target in targets:
        assert definition[0].count(target) == 1


# `tearing_down` is admitted by migration 0168 (#3026).
@pytest.mark.parametrize("system_state", ["ready", "tearing_down"])
@pytest.mark.parametrize("activation", ["prepared", "activating"])
def test_0160_commits_teardown_failure_for_nonfailed_system(
    migrated_url: str, authority_role_dsns: _RoleDsns, activation: str, system_state: str
) -> None:
    case, authority = _admitted(migrated_url, authority_role_dsns, activation, system_state)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(worker, case, authority) == ("applied", "queued")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT s.state, e.state, a.state FROM systems s "
            "JOIN external_boot_activations e ON e.system_id = s.id "
            "JOIN external_boot_authorities a ON a.id = %s WHERE s.id = %s",
            (authority.authority_id, case.system_id),
        ).fetchone() == (system_state, activation, "retired")


def test_0160_terminal_teardown_failure_leaves_the_run(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case, authority = _admitted(migrated_url, authority_role_dsns, "activating")
    with psycopg.connect(migrated_url) as seed:
        seed.execute("UPDATE runs SET state = 'running' WHERE id = %s", (case.run_id,))
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(worker, case, authority, terminal=True) == ("applied", "failed")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state, failure_category FROM runs WHERE id = %s", (case.run_id,)
        ).fetchone() == ("running", None)
        assert conn.execute(
            "SELECT state, error_category FROM jobs WHERE id = %s", (case.job_id,)
        ).fetchone() == ("failed", "infrastructure_failure")


def test_0160_teardown_failure_leaves_reservation_credit(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case, first = _admitted(migrated_url, authority_role_dsns, "prepared")
    with psycopg.connect(migrated_url) as conn:
        before = _credit(conn, case)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(worker, case, first) == ("applied", "queued")
    retry = replace(case, attempt=2)
    with psycopg.connect(migrated_url) as seed:
        seed.execute(
            "UPDATE jobs SET state = 'running', attempt = 2, worker_id = %s, "
            "lease_expires_at = now() + interval '5 minutes', heartbeat_at = now() WHERE id = %s",
            (case.worker_id, case.job_id),
        )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        second = _allocate(worker, retry)
    with psycopg.connect(migrated_url) as seed:
        _acknowledge(seed, retry, second)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(worker, retry, second, attempt=2, terminal=True) == ("applied", "failed")
    with psycopg.connect(migrated_url) as conn:
        assert _credit(conn, case) == before == (("ready", 4096), (0,))


@pytest.mark.parametrize("loss", ["newer_activation", "reclaimed"])
def test_0160_teardown_failure_is_superseded(
    migrated_url: str, authority_role_dsns: _RoleDsns, loss: str
) -> None:
    case, authority = _admitted(migrated_url, authority_role_dsns, "prepared")
    with psycopg.connect(migrated_url) as seed:
        if loss == "reclaimed":
            seed.execute("UPDATE jobs SET attempt = 2 WHERE id = %s", (case.job_id,))
        else:
            newer = uuid4()
            seed.execute(
                "INSERT INTO external_boot_activations "
                "(id, system_id, run_id, plan_identity, operation_owner_id, "
                "authority_generation, state, cleanup_complete, teardown_evidence, "
                "cleanup_evidence, created_at) "
                "SELECT %s, system_id, run_id, %s, %s, 2, 'torn_down', true, %s, %s, "
                "created_at + interval '1 second' FROM external_boot_activations WHERE id = %s",
                (
                    newer,
                    "sha256:" + "e" * 64,
                    uuid4(),
                    Jsonb(
                        {
                            "schema": "external-boot-teardown-evidence-v1",
                            "system_id": str(case.system_id),
                        }
                    ),
                    Jsonb(
                        {
                            "schema": "external-boot-cleanup-evidence-v1",
                            "activation_id": str(newer),
                            "system_id": str(case.system_id),
                            "mode": "pending_system_teardown",
                        }
                    ),
                    case.activation_id,
                ),
            )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(worker, case, authority) == ("superseded", None)
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state FROM external_boot_authorities WHERE id = %s", (authority.authority_id,)
        ).fetchone() == ("current",)


def test_attempt_is_running_reads_the_job_attempt(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown", worker_suffix="r")
    job = Job.model_construct(id=case.job_id, attempt=1)

    async def observe(state: str, attempt: int) -> bool:
        with psycopg.connect(migrated_url) as seed:
            seed.execute(
                "UPDATE jobs SET state = %s, attempt = %s WHERE id = %s",
                (state, attempt, case.job_id),
            )
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_worker")
        ) as worker:
            return await queue.external_boot_attempt_is_running(worker, job)

    assert asyncio.run(observe("running", 1)) is True
    assert asyncio.run(observe("running", 2)) is False
    assert asyncio.run(observe("queued", 1)) is False
