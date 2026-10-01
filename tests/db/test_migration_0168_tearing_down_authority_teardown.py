"""Real-Postgres proofs that the authority teardown admits a `tearing_down` System (#3026)."""

from __future__ import annotations

import re

import psycopg

from kdive.db import migrate
from tests.db.external_boot_authority_support import (
    _ALLOCATE_SIGNATURE,
    _COMMIT_SIGNATURE,
    _PLAN,
    _allocate,
    _apply_through,
    _AuthorityCase,
    _RoleDsns,
)
from tests.db.external_boot_journal_support import (
    _finalize,
    _make_current,
    _proof,
    _ready_teardown_case,
)

_FINALIZE_SIGNATURE = (
    "finalize_external_boot_authority_teardown(bytea,uuid,integer,uuid,bigint,bigint,text,bytea)"
)


def _migration_sql() -> str:
    return next(m for m in migrate.discover_migrations() if m.version == "0168").sql


def _tearing_down_case(migrated_url: str, suffix: str) -> _AuthorityCase:
    case = _ready_teardown_case(migrated_url, suffix)
    with psycopg.connect(migrated_url) as conn:
        conn.execute("UPDATE systems SET state = 'tearing_down' WHERE id = %s", (case.system_id,))
    return case


def test_0168_patch_targets_exist_once(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0167")
    (target,) = re.findall(r"v_old constant text := \$old\$(.*?)\$old\$;", _migration_sql())
    for signature in (_ALLOCATE_SIGNATURE, _FINALIZE_SIGNATURE, _COMMIT_SIGNATURE):
        row = pg_conn.execute(
            "SELECT pg_get_functiondef(%s::regprocedure)", (f"public.{signature}",)
        ).fetchone()
        assert row is not None
        assert row[0].count(target) == 1, signature
        assert "'tearing_down'" not in row[0], signature


def test_0168_tearing_down_teardown_credits_once(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _tearing_down_case(migrated_url, "u")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    proof = _proof(case, "complete_ready")
    with psycopg.connect(migrated_url) as conn:
        digest = _make_current(conn, case, authority, proof, 2)
    assert _finalize(authority_role_dsns, case, authority, proof, 2, digest) == "applied"
    assert _finalize(authority_role_dsns, case, authority, proof, 2, digest) == "applied"
    with psycopg.connect(migrated_url) as conn:
        outcome = conn.execute(
            "SELECT s.state, "
            "(SELECT count(*) FROM external_boot_reservation_releases r "
            " WHERE r.activation_id = %s), "
            "(SELECT count(*) FROM external_boot_reservations r WHERE r.activation_id = %s), "
            "(SELECT array_agg(a.transition) FROM audit_log a "
            " WHERE a.object_id = s.id AND a.tool = 'systems.teardown') "
            "FROM systems s WHERE s.id = %s",
            (case.activation_id, case.activation_id, case.system_id),
        ).fetchone()
    assert outcome == ("torn_down", 1, 0, ["tearing_down->torn_down"])


def test_0168_expired_allocation_still_supersedes(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _tearing_down_case(migrated_url, "w")
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE allocations SET state = 'expired' WHERE id = %s", (case.allocation_id,)
        )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        row = worker.execute(
            "SELECT status FROM allocate_external_boot_authority"
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
    assert row == ("superseded",)
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT s.state, e.state, "
            "(SELECT count(*) FROM external_boot_authorities a WHERE a.system_id = s.id) "
            "FROM systems s JOIN external_boot_activations e ON e.system_id = s.id "
            "WHERE s.id = %s",
            (case.system_id,),
        ).fetchone() == ("tearing_down", "prepared", 0)
