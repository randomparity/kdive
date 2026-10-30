"""Real-Postgres proofs that recover and resolve-conflict failures commit while recovering."""

from __future__ import annotations

import re
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from tests.db.external_boot_authority_support import (
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


def _migration_sql() -> str:
    return next(m for m in migrate.discover_migrations() if m.version == "0185").sql


def _case(
    migrated_url: str, purpose: str, suffix: str, *, recovering: bool, max_attempts: int = 3
) -> _AuthorityCase:
    """Seed one running ``purpose`` job; ``recovering`` gives its activation a live attempt."""
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose=purpose, worker_suffix=suffix)
        _prepare_purpose_state(seed, case, purpose)
        seed.execute("UPDATE jobs SET max_attempts = %s WHERE id = %s", (max_attempts, case.job_id))
        seed.execute(
            "INSERT INTO remote_module_attempt_obligations (system_id, run_id, operation_nonce) "
            "VALUES (%s, %s, %s)",
            (case.system_id, case.run_id, uuid4().hex),
        )
        if recovering:
            attempt_id = uuid4()
            seed.execute(
                "INSERT INTO external_boot_recovery_attempts "
                "(activation_id, attempt_number, attempt_id, authority_generation, "
                "recovery_basis, recovery_readiness_deadline, state) "
                "SELECT %s, coalesce(max(attempt_number), 0) + 1, %s, 1, 'recovery_point', "
                "now() + interval '5 minutes', 'recovering' "
                "FROM external_boot_recovery_attempts WHERE activation_id = %s",
                (case.activation_id, attempt_id, case.activation_id),
            )
            seed.execute(
                "UPDATE external_boot_activations SET state = 'recovering', "
                "current_attempt_id = %s WHERE id = %s",
                (attempt_id, case.activation_id),
            )
    return case


def _authority(role_dsns: _RoleDsns, migrated_url: str, case: _AuthorityCase) -> _Allocated:
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(migrated_url) as seed:
        seed.execute(
            "UPDATE external_boot_authorities SET state = 'current', acknowledged_at = now() "
            "WHERE id = %s",
            (authority.authority_id,),
        )
        seed.execute(
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
    return authority


def _fail(
    role_dsns: _RoleDsns,
    case: _AuthorityCase,
    authority: _Allocated,
    *,
    terminal: bool,
    category: str = "infrastructure_failure",
    context: dict[str, str] | None = None,
) -> tuple[str, str | None]:
    result = {
        "schema": "external-boot-authority-result-v1",
        "operation": "fail",
        "error_category": category,
        "failure_context": context or {"phase": "provider-call"},
        "terminal": terminal,
    }
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        row = worker.execute(
            "SELECT status, job_state FROM commit_external_boot_authority_result("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                case.purpose,
                Jsonb(result),
            ),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _states(migrated_url: str, case: _AuthorityCase) -> tuple[object, ...]:
    """Activation, its outcome, current attempt, its outcome, job, and the obligation discharge."""
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT e.state, e.terminal_evidence ->> 'outcome', ra.state, "
            "ra.terminal_evidence ->> 'outcome', j.state, o.mutation_discharge_reason "
            "FROM external_boot_activations AS e "
            "LEFT JOIN external_boot_recovery_attempts AS ra "
            "ON ra.activation_id = e.id AND ra.attempt_id = e.current_attempt_id "
            "JOIN jobs AS j ON j.id = %s "
            "JOIN remote_module_attempt_obligations AS o ON o.system_id = e.system_id "
            "WHERE e.id = %s",
            (case.job_id, case.activation_id),
        ).fetchone()
    assert row is not None
    return tuple(row)


def test_0185_patch_targets_exist_once(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0184")
    definition = pg_conn.execute(
        "SELECT pg_get_functiondef(%s::regprocedure)", (f"public.{_COMMIT_SIGNATURE}",)
    ).fetchone()
    assert definition is not None
    targets = re.findall(r"v_old_\w+ constant text := \$old\$(.*?)\$old\$;", _migration_sql(), re.S)
    assert len(targets) == 3
    for target in targets:
        assert definition[0].count(target) == 1, target


def test_resolve_conflict_non_terminal_failure_while_recovering_requeues(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url, "resolve-conflict", "a", recovering=True)
    authority = _authority(authority_role_dsns, migrated_url, case)
    assert _fail(authority_role_dsns, case, authority, terminal=False) == ("applied", "queued")
    assert _states(migrated_url, case) == (
        "recovering",
        None,
        "recovering",
        None,
        "queued",
        None,
    )


@pytest.mark.parametrize(
    ("purpose", "terminal", "max_attempts", "suffix"),
    [
        ("recover", True, 3, "b"),
        ("resolve-conflict", True, 3, "c"),
        ("recover", False, 1, "d"),
        ("resolve-conflict", False, 1, "e"),
    ],
    ids=[
        "recover-terminal",
        "resolve-conflict-terminal",
        "recover-last-attempt",
        "resolve-conflict-last-attempt",
    ],
)
def test_terminal_failure_while_recovering_closes_the_recovery(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    purpose: str,
    terminal: bool,
    max_attempts: int,
    suffix: str,
) -> None:
    case = _case(migrated_url, purpose, suffix, recovering=True, max_attempts=max_attempts)
    authority = _authority(authority_role_dsns, migrated_url, case)
    assert _fail(authority_role_dsns, case, authority, terminal=terminal) == ("applied", "failed")
    assert _states(migrated_url, case) == (
        "recovery_failed",
        "recovery_failed",
        "failed",
        "recovery_failed",
        "failed",
        "terminal_escape",
    )


def test_recover_readiness_failure_before_recovery_began_commits(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url, "recover", "f", recovering=False)
    authority = _authority(authority_role_dsns, migrated_url, case)
    assert _fail(
        authority_role_dsns, case, authority, terminal=True, category="readiness_failure"
    ) == ("applied", "failed")
    assert _states(migrated_url, case) == ("active", "active", None, None, "failed", None)


def test_repeated_provider_conflict_while_recovering_closes_the_recovery(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """ADR-0714's repeat rule makes the second identical conflict terminal, so it closes too."""
    conflict = {"phase": "commit", "authority_reason": "provider-conflict"}
    case = _case(migrated_url, "resolve-conflict", "g", recovering=True)
    authority = _authority(authority_role_dsns, migrated_url, case)
    assert _fail(authority_role_dsns, case, authority, terminal=False, context=conflict) == (
        "applied",
        "queued",
    )
    retry = replace(case, attempt=2)
    with psycopg.connect(migrated_url) as seed:
        seed.execute(
            "UPDATE jobs SET state = 'running', attempt = 2, worker_id = %s, "
            "lease_expires_at = now() + interval '5 minutes', heartbeat_at = now() "
            "WHERE id = %s",
            (case.worker_id, case.job_id),
        )
    authority = _authority(authority_role_dsns, migrated_url, retry)
    assert _fail(authority_role_dsns, retry, authority, terminal=False, context=conflict) == (
        "applied",
        "failed",
    )
    assert _states(migrated_url, case)[:5] == (
        "recovery_failed",
        "recovery_failed",
        "failed",
        "recovery_failed",
        "failed",
    )
