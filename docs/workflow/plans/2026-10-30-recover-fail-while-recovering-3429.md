# Recover and resolve-conflict failures commit while recovering (#3429) — plan

Goal: a recover or resolve-conflict failure commit is admitted while the activation is
`recovering`, and a terminal one closes the recovery `recovery_failed`, so the job no longer ends
`lease_expired`.

Architecture: one in-place rewrite of `commit_external_boot_authority_result` in migration 0185,
using the 0181 pattern (exact-text replacement, each target checked to occur once). No Python
source changes. Spec:
[2026-10-30-recover-fail-while-recovering-3429-design.md](../specs/2026-10-30-recover-fail-while-recovering-3429-design.md).

Tech stack: PostgreSQL 17 PL/pgSQL; Python 3.14 pytest with the disposable Postgres fixtures in
`tests/db/conftest.py` (`migrated_url`, `pg_conn`, `authority_role_dsns`).

Expected implementation size: 350–370 changed lines (M) — the 0185 migration (89), one new test
module (about 260), seven window-list rows and four slice edits.

## Global Constraints

- Migration number 0185 is assigned by the campaign. Do not create 0183 or 0184.
- Applied migrations are byte-immutable (ADR-0015): never edit 0181 or earlier files.
- Lines ≤ 100 characters (`just lint`); `just type` checks tests too.
- The release `fail` arm and the worker's SUPERSEDED handling are out of scope.

## File map

| File | Change | Owns |
|---|---|---|
| `src/kdive/db/schema/0185_recover_fail_while_recovering.sql` | create | the fail admission and terminal close rewrite (C1–C3, C5) |
| `tests/db/test_migration_0185_recover_fail_while_recovering.py` | create | connected proofs C1–C5 |
| `tests/db/test_migrate.py` | modify | three version lists and one `(version, filename)` list gain the 0185 row |
| `tests/db/test_migration_0091_system_object_sweep_cursors.py`, `tests/db/test_migration_0102_build_gc_cursors.py`, `tests/db/test_migration_0115_capture_reap_state.py` | modify | each `(version, filename)` window list gains the 0185 row |

Obsolete path removed: the recover-only readiness branch inside the `fail` branch of
`commit_external_boot_authority_result` (removed by the migration text replacement). No callers
change: the worker already calls `queue.fail_external_boot` and handles `applied`.

## Task 1: failure commits while recovering

**Interfaces.** Consumes from `tests/db/external_boot_authority_support.py` (verified on main
237f78212): `_seed_case(conn, *, purpose, worker_suffix) -> _AuthorityCase`,
`_prepare_purpose_state(conn, case, purpose)`, `_allocate(worker, case) -> _Allocated`,
`_apply_through(conn, version)`, and the constants `_COMMIT_SIGNATURE`, `_JOURNAL`, `_PLAN`,
`_QUIESCENCE`, and the types `_AuthorityCase`, `_Allocated`, `_RoleDsns`. Provides nothing to
later tasks.

**Verification.**

- Contract C1+C4. Mode: focused-test.
  `test_resolve_conflict_non_terminal_failure_while_recovering_requeues`. Red: the commit returns
  `('superseded', None)`.
- Contract C2. Mode: focused-test. `test_terminal_failure_while_recovering_closes_the_recovery`
  (four ids). Red: resolve-conflict ids return `superseded`; `recover-terminal` and
  `recover-last-attempt` leave the activation `recovering`.
- Contract C2, repeat rule. Mode: focused-test.
  `test_repeated_provider_conflict_while_recovering_closes_the_recovery`. Red: the first commit
  returns `('superseded', None)`.
- Contract C3. Mode: focused-test. `test_recover_readiness_failure_before_recovery_began_commits`.
  Red: `('superseded', None)`.
- Contract C5. Mode: focused-test. `test_0185_patch_targets_exist_once`. Red: `StopIteration`
  while the migration file does not exist.
- Migration window lists. Mode: focused-test. `uv run python -m pytest tests/db/test_migrate.py
  tests/db/test_migration_0091_system_object_sweep_cursors.py
  tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py
  -q`. Red: once the migration exists, each list misses `0185`.

**Steps.**

1. Create `tests/db/test_migration_0185_recover_fail_while_recovering.py`:

```python
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
```

2. Run `uv run python -m pytest tests/db/test_migration_0185_recover_fail_while_recovering.py -q`.
   Expect 8 failures: the seven behaviour cases as listed above and the patch-target test with
   `StopIteration`. Commit the test (`test(db): ...`).
3. Create `src/kdive/db/schema/0185_recover_fail_while_recovering.sql`:

```sql
-- ADR-0714 amendment (#3429): a recover or resolve-conflict failure commits while recovering, and
-- a terminal one closes the recovery as 0181 does for release.
--
-- The resolve-conflict `fail` admission named only `recovery_conflict`, but the job's own
-- `recovery-attempt` commit moves the activation to `recovering`, so every later failure was
-- refused and the job stayed `running` until its lease lapsed.  The admission now names the states
-- allocation already admits for the purpose.  The release-only terminal close of 0181 now also
-- covers recover and resolve-conflict, which subsumes the recover readiness branch: that branch
-- refused a readiness failure that arrived before any recovery attempt existed.
-- The function is rewritten in place (as 0128, 0143, 0170 and 0181 do) so earlier patches survive.
DO $$
DECLARE
    v_commit constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_old_fail constant text := $old$           OR (p_purpose = 'resolve-conflict' AND (
               v_system.state NOT IN ('ready', 'crashed', 'failed')
               OR v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state IS DISTINCT FROM 'recovery_conflict'
           ))$old$;
    v_new_fail constant text := $new$           OR (p_purpose = 'resolve-conflict' AND (
               v_system.state NOT IN ('ready', 'crashed', 'failed')
               OR v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state NOT IN ('recovery_conflict', 'recovering')
           ))$new$;
    -- The recover readiness branch, removed: the widened terminal close below covers it, and its
    -- `superseded` return refused a readiness failure on an activation that never began recovery.
    v_old_readiness constant text := $old$        ELSIF p_purpose = 'recover'
           AND p_result ->> 'error_category' = 'readiness_failure'
           AND (p_result ->> 'terminal')::boolean THEN
            v_evidence := jsonb_build_object(
                'schema', 'external-boot-terminal-evidence-v1',
                'activation_id', p_activation_id::text,
                'system_id', p_system_id::text,
                'outcome', 'recovery_failed',
                'composite_state', v_ack.positive_quiescence_digest,
                'objects', '[]'::jsonb,
                'observed_at', to_jsonb(clock_timestamp())
            );
            UPDATE public.external_boot_recovery_attempts
            SET state = 'failed', terminal_evidence = v_evidence,
                conflict_evidence = NULL
            WHERE activation_id = p_activation_id
              AND attempt_id = v_activation.current_attempt_id
              AND state = 'recovering';
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'superseded'::text, NULL::text;
                RETURN;
            END IF;
            UPDATE public.external_boot_activations
            SET state = 'recovery_failed', terminal_evidence = v_evidence
            WHERE id = p_activation_id AND state = 'recovering';
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'superseded'::text, NULL::text;
                RETURN;
            END IF;
            UPDATE public.remote_module_attempt_obligations
            SET mutation_discharged_at = now(), mutation_discharge_reason = 'terminal_escape'
            WHERE system_id = p_system_id AND mutation_discharged_at IS NULL;
        END IF;$old$;
    v_new_readiness constant text := $new$        END IF;$new$;
    v_old_close constant text := $old$        IF v_terminal AND p_purpose = 'release'
           AND v_activation.state = 'recovering' THEN$old$;
    v_new_close constant text := $new$        IF v_terminal
           AND p_purpose IN ('release', 'recover', 'resolve-conflict')
           AND v_activation.state = 'recovering' THEN$new$;
    v_definition text;
BEGIN
    v_definition := pg_get_functiondef(v_commit);
    IF (length(v_definition) - length(replace(v_definition, v_old_fail, ''))) / length(v_old_fail)
       <> 1 THEN
        RAISE EXCEPTION 'external boot resolve-conflict fail admission shape changed';
    END IF;
    IF (length(v_definition) - length(replace(v_definition, v_old_readiness, '')))
       / length(v_old_readiness) <> 1 THEN
        RAISE EXCEPTION 'external boot recover readiness failure shape changed';
    END IF;
    IF (length(v_definition) - length(replace(v_definition, v_old_close, '')))
       / length(v_old_close) <> 1 THEN
        RAISE EXCEPTION 'external boot terminal recovery close shape changed';
    END IF;
    EXECUTE replace(
        replace(
            replace(v_definition, v_old_fail, v_new_fail),
            v_old_readiness, v_new_readiness
        ),
        v_old_close, v_new_close
    );
END
$$;
```

4. Re-run the step 2 command. Expect `8 passed`. Then run
   `uv run python -m pytest tests/db/test_release_fail_recovering.py tests/db/test_external_boot_authority_migration.py -q`
   and expect all tests to pass. These suites cover release and the existing recover and
   resolve-conflict behaviour.
5. In `tests/db/test_migrate.py`, add `"0185",` after each of the three `"0182",` version-list
   entries, and add `("0185", "0185_recover_fail_while_recovering.sql"),` after the
   `("0182", ...)` tuple. In each of the 0091, 0102 and 0115 window tests, add the same tuple after
   its `("0182", ...)` tuple. Raise each `migrations[-K:]` slice by one: `test_migrate.py` 79→80,
   0091 75→76, 0102 75→76, 0115 62→63. Run the window command above and expect it to pass.
6. Run `just lint` and `just type` (expect exit 0), then commit (`fix(external-boot): ...`).

**Acceptance.** All eight new cases pass; the release and existing authority migration suites
stay green; the four window tests pass.

**Rollback.** Delete the 0185 file and the test module, and revert the window rows. No applied
migration changes.

## Notes for the orchestrator

0183 (#3423) and 0184 (#3432) land first. The patch-target test applies through `0184`, so it
checks against whatever function definition those migrations produce. If either of them rewrites
the same `commit_external_boot_authority_result` text, 0185's shape check raises an error, and the
targets must be re-derived from that definition. The window lists need 0183 and 0184 inserted
before 0185 after the base refresh.
