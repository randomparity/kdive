"""Real-Postgres proofs that the authority teardown runs on an ended Allocation (#2992)."""

from __future__ import annotations

import re

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from tests.db.external_boot_authority_support import (
    _ACKNOWLEDGE_SIGNATURE,
    _ALLOCATE_SIGNATURE,
    _COMMIT_SIGNATURE,
    _JOURNAL,
    _PLAN,
    _QUIESCENCE,
    _allocate,
    _Allocated,
    _apply_through,
    _AuthorityCase,
    _prepare_purpose_state,
    _RoleDsns,
    _seed_case,
)
from tests.db.external_boot_journal_support import (
    _finalize,
    _make_current,
    _proof,
    _ready_teardown_case,
)

_OTHER_PURPOSES = ("activate", "recover", "resolve-conflict", "release")
_FAILURE = {
    "schema": "external-boot-authority-result-v1",
    "operation": "fail",
    "error_category": "infrastructure_failure",
    "failure_context": {"phase": "provider-call"},
    "terminal": False,
}


def _migration_sql() -> str:
    return next(m for m in migrate.discover_migrations() if m.version == "0169").sql


def _end(migrated_url: str, case: _AuthorityCase, state: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        conn.execute("UPDATE allocations SET state = %s WHERE id = %s", (state, case.allocation_id))


def _try_allocate(role_dsns: _RoleDsns, case: _AuthorityCase) -> str:
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        row = worker.execute(
            f"SELECT status FROM {_ALLOCATE_SIGNATURE.split('(')[0]}"
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
                case.activation_id,
                case.run_id,
                case.system_id,
                _PLAN,
                case.purpose,
                case.provider_kind,
                case.authority_instance,
                case.operation_identity,
            ),
        ).fetchone()
    assert row is not None
    return row[0]


def _acknowledge(role_dsns: _RoleDsns, case: _AuthorityCase, authority: _Allocated) -> str:
    with psycopg.connect(role_dsns("kdive_provider_authority"), autocommit=True) as host:
        row = host.execute(
            f"SELECT status FROM {_ACKNOWLEDGE_SIGNATURE.split('(')[0]}"
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                authority.authority_id,
                authority.generation,
                case.allocation_id,
                case.activation_id,
                case.run_id,
                case.system_id,
                _PLAN,
                case.job_id,
                case.attempt,
                case.purpose,
                case.provider_kind,
                case.authority_instance,
                case.worker_id,
                case.operation,
                case.operation_identity,
                authority.operation_digest,
                1,
                _JOURNAL,
                _QUIESCENCE,
            ),
        ).fetchone()
    assert row is not None
    return row[0]


def _commit_failure(
    role_dsns: _RoleDsns, case: _AuthorityCase, authority: _Allocated
) -> tuple[str, str | None]:
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        row = worker.execute(
            f"SELECT status, job_state FROM {_COMMIT_SIGNATURE.split('(')[0]}"
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
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
                case.operation,
                Jsonb(_FAILURE),
            ),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _seed_other(migrated_url: str, purpose: str, suffix: str) -> _AuthorityCase:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, purpose=purpose, worker_suffix=suffix)
        _prepare_purpose_state(conn, case, purpose)
    return case


def _lifecycle(migrated_url: str, case: _AuthorityCase) -> tuple[object, ...]:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT e.state, s.state FROM external_boot_activations e "
            "JOIN systems s ON s.id = e.system_id WHERE e.id = %s",
            (case.activation_id,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def test_0169_patch_target_exists_once(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0168")
    sql = _migration_sql()
    (old,) = re.findall(r"v_old constant text := \$old\$(.*?)\$old\$;", sql)
    (new,) = re.findall(r"v_new constant text := \$new\$(.*?)\$new\$;", sql)
    for signature in (_ALLOCATE_SIGNATURE, _ACKNOWLEDGE_SIGNATURE, _COMMIT_SIGNATURE):
        row = pg_conn.execute(
            "SELECT pg_get_functiondef(%s::regprocedure)", (f"public.{signature}",)
        ).fetchone()
        assert row is not None
        assert row[0].count(old) == 1, signature
        assert new not in row[0], signature


# The `expired` + `tearing_down` arm inverts the 0168 proof that this case answered `superseded`.
@pytest.mark.parametrize(
    ("allocation_state", "system_state", "suffix"),
    [("released", "failed", "x"), ("expired", "tearing_down", "y")],
)
def test_0169_teardown_completes_on_ended_allocation(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    allocation_state: str,
    system_state: str,
    suffix: str,
) -> None:
    case = _ready_teardown_case(migrated_url, suffix)
    with psycopg.connect(migrated_url) as conn:
        conn.execute("UPDATE systems SET state = %s WHERE id = %s", (system_state, case.system_id))
    _end(migrated_url, case, allocation_state)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    assert _acknowledge(authority_role_dsns, case, authority) == "applied"
    proof = _proof(case, "complete_ready")
    with psycopg.connect(migrated_url) as conn:
        digest = _make_current(conn, case, authority, proof, 2, acknowledge=False)
    assert _finalize(authority_role_dsns, case, authority, proof, 2, digest) == "applied"
    assert _finalize(authority_role_dsns, case, authority, proof, 2, digest) == "applied"
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT s.state, a.state, "
            "(SELECT count(*) FROM external_boot_reservation_releases r "
            " WHERE r.activation_id = %s), "
            "(SELECT count(*) FROM external_boot_reservations r WHERE r.activation_id = %s), "
            "(SELECT array_agg(l.transition) FROM audit_log l "
            " WHERE l.object_id = s.id AND l.tool = 'systems.teardown') "
            "FROM systems s JOIN allocations a ON a.id = s.allocation_id WHERE s.id = %s",
            (case.activation_id, case.activation_id, case.system_id),
        ).fetchone() == ("torn_down", allocation_state, 1, 0, [f"{system_state}->torn_down"])


@pytest.mark.parametrize(("allocation_state", "suffix"), [("released", "p"), ("expired", "q")])
def test_0169_teardown_failure_commits_on_ended_allocation(
    migrated_url: str, authority_role_dsns: _RoleDsns, allocation_state: str, suffix: str
) -> None:
    case = _ready_teardown_case(migrated_url, suffix)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    assert _acknowledge(authority_role_dsns, case, authority) == "applied"
    before = _lifecycle(migrated_url, case)
    _end(migrated_url, case, allocation_state)
    assert _commit_failure(authority_role_dsns, case, authority) == ("applied", "queued")
    assert _lifecycle(migrated_url, case) == before


@pytest.mark.parametrize("purpose", _OTHER_PURPOSES)
@pytest.mark.parametrize("allocation_state", ["active", "expired"])
def test_0169_allocate_keeps_fence_for_other_purposes(
    migrated_url: str, authority_role_dsns: _RoleDsns, purpose: str, allocation_state: str
) -> None:
    case = _seed_other(migrated_url, purpose, "a")
    _end(migrated_url, case, allocation_state)
    admitted = allocation_state == "active"
    assert _try_allocate(authority_role_dsns, case) == ("allocated" if admitted else "superseded")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM external_boot_authorities WHERE system_id = %s",
            (case.system_id,),
        ).fetchone() == (int(admitted),)


@pytest.mark.parametrize("purpose", _OTHER_PURPOSES)
@pytest.mark.parametrize("allocation_state", ["active", "expired"])
def test_0169_acknowledge_keeps_fence_for_other_purposes(
    migrated_url: str, authority_role_dsns: _RoleDsns, purpose: str, allocation_state: str
) -> None:
    case = _seed_other(migrated_url, purpose, "b")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    _end(migrated_url, case, allocation_state)
    admitted = allocation_state == "active"
    assert _acknowledge(authority_role_dsns, case, authority) == (
        "applied" if admitted else "superseded"
    )
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT a.state, (SELECT count(*) FROM external_boot_authority_acknowledgements k "
            "WHERE k.authority_id = a.id) FROM external_boot_authorities a WHERE a.id = %s",
            (authority.authority_id,),
        ).fetchone() == ("current" if admitted else "allocating", int(admitted))


@pytest.mark.parametrize("purpose", _OTHER_PURPOSES)
def test_0169_commit_keeps_fence_for_other_purposes(
    migrated_url: str, authority_role_dsns: _RoleDsns, purpose: str
) -> None:
    case = _seed_other(migrated_url, purpose, "c")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    assert _acknowledge(authority_role_dsns, case, authority) == "applied"
    before = _lifecycle(migrated_url, case)
    _end(migrated_url, case, "expired")
    assert _commit_failure(authority_role_dsns, case, authority) == (
        "authority_superseded",
        "failed",
    )
    assert _lifecycle(migrated_url, case) == before
