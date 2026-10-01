# A repeated provider-conflict ends the external-boot job (#2901) — implementation plan

Goal: when an authority-driven external-boot job gets the same `provider-conflict` on two
consecutive attempts in one budget, the commit makes the job terminal instead of requeueing it.

Spec: [2026-10-01-provider-conflict-churn-design.md](../specs/2026-10-01-provider-conflict-churn-design.md).
Decision: [ADR-0714](../../adr/0714-repeated-provider-conflict-ends-the-external-boot-job.md).

Architecture:

- Migration `0170` adds a nullable `failure_context` column to `external_boot_authority_audit`
  and patches `commit_external_boot_authority_result` in place (the 0160 pattern). The patch
  admits and validates a new context key, `authority_reason`. It stores a `fail` result's context
  on the audit row. It makes a `fail` terminal when the previous attempt in the same budget was
  requeued with an equal context that carries `authority_reason`.
- On the worker side, `provider-conflict` is marked in the sender error's details, and
  `_bound_failure` carries the mark into the result.

Tech stack: Postgres plpgsql, Python 3.14, pydantic 2, psycopg 3, pytest.

Expected implementation size: 230–300 changed lines (M) — from the file map below: migration
~95, sender ~3, models ~12, runner ~10, migration tests ~110, sender test ~20, runner test ~40,
model test ~15, migration lists ~4.

## Global Constraints

- Migration file `src/kdive/db/schema/0170_repeated_provider_conflict_terminal.sql`; ADR
  `docs/adr/0714-*.md`. Both numbers are assigned by the campaign; do not renumber them. Applied
  migrations are byte-immutable (ADR-0015). Sibling #2992 holds `0169`. After it merges, the
  branch base is refreshed, and the `tests/db/test_migrate.py` lists gain `0169` before `0170`.
- Do not change the authority wire categories (`transport.py`, `_PEER_REASONS`),
  `COMMITTABLE_ERROR_CATEGORIES`, `worker._is_terminal`, `max_attempts`, or any ADR-0711 grant
  function.
- Doc style (AGENTS.md): plain prose; do not use "critical", "robust", "comprehensive", or
  "elegant".
- Guardrails: `just lint`, `just type`, focused `just test-verbose <paths>`,
  `git fetch origin main && just records`. The pre-push hook runs `just ci`; run it as
  `just ci > <file> 2>&1 < /dev/null`.
- Commit the implementation before any controlled-fault arm, because the revert step restores to
  HEAD.

## File map

| File | Today | After |
|---|---|---|
| `src/kdive/db/schema/0170_repeated_provider_conflict_terminal.sql` | — | audit column and index; four exact-once commit-function patches |
| `src/kdive/jobs/authority_sender.py` | `_failure` sets details only for remote-module reasons | also `{"authority_reason": "provider-conflict"}` |
| `src/kdive/jobs/models.py` | `ExternalBootAuthorityFailureContext` has phase/reason/next_action/cmdline_mismatch | adds `authority_reason`; `_FailureResult` requires `infrastructure_failure` with it |
| `src/kdive/jobs/handlers/external_boot/runner.py` | `_bound_failure` context is `{phase, cmdline_mismatch?}` | copies `authority_reason` from the error details |
| `tests/db/test_migration_0170_repeated_provider_conflict.py` | — | S1–S4 against real Postgres |
| `tests/db/test_migrate.py` | four migration lists end at `0168` | end at `0170` |
| `tests/jobs/test_external_boot_authority_client.py` | — | the sender marks only `provider-conflict` (S5) |
| `tests/jobs/test_external_boot_authority_models.py` | — | validator arms |
| `tests/jobs/handlers/external_boot/test_runner.py` | — | the runner carries the mark, and two commits end the job (S5, S1 end to end) |

## Task 1: migration 0170

Files: create `src/kdive/db/schema/0170_repeated_provider_conflict_terminal.sql` and
`tests/db/test_migration_0170_repeated_provider_conflict.py`; modify `tests/db/test_migrate.py`.

Interfaces: produces the SQL contract that Task 2 depends on. The commit function accepts
`failure_context.authority_reason = "provider-conflict"` only with
`error_category = "infrastructure_failure"`, and makes the second identical consecutive one
terminal. The test helpers come from `tests/db/external_boot_authority_support.py`
(`_seed_case(conn, *, purpose, worker_suffix)`, `_allocate(worker, case)`,
`_apply_through(conn, version)`, `_COMMIT_SIGNATURE`, `_JOURNAL`, `_PLAN`, `_QUIESCENCE`,
`_RoleDsns`, `_AuthorityCase`, `_Allocated`). Those helpers exist and are used by
`tests/db/test_migration_0160_external_boot_teardown_failure_commit.py`. `_seed_case` seeds the
job at `attempt = 1`, `max_attempts = 3`.

Verification:

- Mode: focused-test — patch targets. `test_0170_patch_targets_exist_once` asserts that each
  `$old$...$old$` text occurs exactly once in the commit function's definition after
  `_apply_through(conn, "0169")`. Red: the file does not exist (StopIteration). Green:
  `uv run python -m pytest tests/db/test_migration_0170_repeated_provider_conflict.py -q`.
- Mode: focused-test — S1 repeat is terminal. `test_0170_second_identical_provider_conflict_is_terminal`:
  attempt 1 commits `("applied", "queued")`, and attempt 2 with the same context commits
  `("applied", "failed")`. The job row is `failed / infrastructure_failure` with the context.
  Red before the patch: attempt 2 returns `queued`.
- Mode: focused-test — S2 still retryable. `test_0170_non_identical_failures_still_requeue`, with
  parameters `no-reason` (both attempts carry `{"phase": "commit"}` only), `phase-differs`
  (attempt 1 at `provider-call`, attempt 2 at `commit`), and `older-budget` (the job's
  `created_at` is moved past the first audit row before attempt 2). Each attempt-2 commit returns
  `queued`.
- Mode: focused-test — S3 credit. `test_0170_terminal_repeat_leaves_reservation_credit` reuses the
  0160 `_credit` read. Credit and releases equal the values before the commits, which is the
  0160 terminal-teardown shape.
- Mode: focused-test — S4 refusal. `test_0170_refuses_an_unknown_or_miscategorized_reason`, with
  parameters (reason `other`, category `infrastructure_failure`) and (reason
  `provider-conflict`, category `boot_timeout`). The commit raises `psycopg.errors.InvalidParameterValue`
  (SQLSTATE `22023`).
- Mode: focused-test — migration registry. The `tests/db/test_migrate.py` lists that end in
  `"0168"` gain `"0170"`, and the filename list gains
  `("0170", "0170_repeated_provider_conflict_terminal.sql")`. Red before the edit, because the
  discovered list contains `0170`. Green:
  `uv run python -m pytest tests/db/test_migrate.py -q`.

Steps:

1. Write the test file below and run it. Expect red: `StopIteration` from `_migration_sql`.
2. Write the migration below. Run the test file. Expect `9 passed`.
3. Add `"0170"` after `"0168"` in each `tests/db/test_migrate.py` list that ends in `"0168"`, and
   `("0170", "0170_repeated_provider_conflict_terminal.sql")` after the `0168` filename tuple.
   Find them with `rg -n '"0168"' tests/db/test_migrate.py`. Run
   `uv run python -m pytest tests/db/test_migrate.py -q`. Expect all tests to pass.
4. `just lint && just type`, then commit `feat(db): end an external-boot job on a repeated provider-conflict (#2901)`.

Migration (complete):

```sql
-- ADR-0714 (#2901): a provider-conflict that repeats identically on consecutive attempts of one
-- external-boot job ends the job instead of allocating another authority generation.  The
-- requeue clears the job's failure context (0163), so the audit row keeps it; the commit then
-- compares this failure with the previous attempt's under the job-row lock it already holds.
ALTER TABLE public.external_boot_authority_audit ADD COLUMN failure_context jsonb;

CREATE INDEX external_boot_authority_audit_job_attempt_idx
    ON public.external_boot_authority_audit (job_id, job_attempt);

DO $$
DECLARE
    v_function constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old_fields constant text := $old$'phase', 'reason', 'next_action', 'cmdline_mismatch'
           )$old$;
    v_new_fields constant text := $new$'phase', 'reason', 'next_action', 'cmdline_mismatch',
               'authority_reason'
           )$new$;
    v_old_phase constant text := $old$           OR (
               v_failure_context ? 'phase'$old$;
    v_new_phase constant text := $new$           OR (
               v_failure_context ? 'authority_reason'
               AND (
                   v_failure_context -> 'authority_reason'
                       IS DISTINCT FROM '"provider-conflict"'::jsonb
                   OR p_result ->> 'error_category' IS DISTINCT FROM 'infrastructure_failure'
               )
           )
           OR (
               v_failure_context ? 'phase'$new$;
    v_old_terminal constant text := $old$v_terminal := (p_result ->> 'terminal')::boolean OR v_job.attempt >= v_job.max_attempts;$old$;
    v_new_terminal constant text := $new$v_terminal := (p_result ->> 'terminal')::boolean
            OR v_job.attempt >= v_job.max_attempts
            OR (
                v_failure_context ? 'authority_reason'
                AND EXISTS (
                    SELECT 1 FROM public.external_boot_authority_audit AS prior
                    WHERE prior.job_id = p_job_id
                      AND prior.job_attempt = v_job.attempt - 1
                      AND prior.created_at >= v_job.created_at
                      AND prior.outcome = 'result_requeued'
                      AND prior.failure_context = v_failure_context
                )
            );$new$;
    v_old_audit constant text := $old$journal_sequence, journal_digest, outcome
    ) VALUES ($old$;
    v_new_audit constant text := $new$journal_sequence, journal_digest, outcome, failure_context
    ) VALUES ($new$;
    v_old_values constant text := $old$p_operation_digest, p_journal_sequence, p_journal_digest, v_outcome
    );$old$;
    v_new_values constant text := $new$p_operation_digest, p_journal_sequence, p_journal_digest, v_outcome,
        CASE WHEN v_operation = 'fail' THEN v_failure_context END
    );$new$;
    v_old text;
BEGIN
    FOREACH v_old IN ARRAY ARRAY[
        v_old_fields, v_old_phase, v_old_terminal, v_old_audit, v_old_values
    ] LOOP
        IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
        THEN
            RAISE EXCEPTION 'external boot commit shape changed near: %', left(v_old, 60);
        END IF;
    END LOOP;
    v_definition := replace(v_definition, v_old_fields, v_new_fields);
    v_definition := replace(v_definition, v_old_phase, v_new_phase);
    v_definition := replace(v_definition, v_old_terminal, v_new_terminal);
    v_definition := replace(v_definition, v_old_audit, v_new_audit);
    v_definition := replace(v_definition, v_old_values, v_new_values);
    EXECUTE v_definition;
END
$$;
```

Test file (complete):

```python
"""Real-Postgres proofs that a repeated provider-conflict ends the external-boot job (#2901)."""

from __future__ import annotations

import re
from dataclasses import replace

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
    _RoleDsns,
    _seed_case,
)

_CONFLICT = {"phase": "commit", "authority_reason": "provider-conflict"}


def _migration_sql() -> str:
    return next(m for m in migrate.discover_migrations() if m.version == "0170").sql


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


def _attempt(
    migrated_url: str, role_dsns: _RoleDsns, case: _AuthorityCase, attempt: int
) -> tuple[_AuthorityCase, _Allocated]:
    """Claim ``attempt`` for the seeded job, then allocate and acknowledge its authority."""
    claimed = replace(case, attempt=attempt)
    if attempt > 1:
        with psycopg.connect(migrated_url) as seed:
            seed.execute(
                "UPDATE jobs SET state = 'running', attempt = %s, worker_id = %s, "
                "lease_expires_at = now() + interval '5 minutes', heartbeat_at = now() "
                "WHERE id = %s",
                (attempt, case.worker_id, case.job_id),
            )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, claimed)
    with psycopg.connect(migrated_url) as seed:
        _acknowledge(seed, claimed, authority)
    return claimed, authority


def _fail(
    role_dsns: _RoleDsns,
    case: _AuthorityCase,
    authority: _Allocated,
    context: dict[str, str],
    category: str = "infrastructure_failure",
) -> tuple[str, str | None]:
    result = {
        "schema": "external-boot-authority-result-v1",
        "operation": "fail",
        "error_category": category,
        "failure_context": context,
        "terminal": False,
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


def _teardown_case(migrated_url: str, suffix: str) -> _AuthorityCase:
    with psycopg.connect(migrated_url) as seed:
        return _seed_case(seed, purpose="teardown", worker_suffix=suffix)


def test_0170_patch_targets_exist_once(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0169")
    definition = pg_conn.execute(
        "SELECT pg_get_functiondef(%s::regprocedure)", (f"public.{_COMMIT_SIGNATURE}",)
    ).fetchone()
    assert definition is not None
    targets = re.findall(r"v_old_\w+ constant text := \$old\$(.*?)\$old\$;", _migration_sql(), re.S)
    assert len(targets) == 5
    for target in targets:
        assert definition[0].count(target) == 1, target


def test_0170_second_identical_provider_conflict_is_terminal(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _teardown_case(migrated_url, "a")
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)
    assert _fail(authority_role_dsns, first, authority, _CONFLICT) == ("applied", "queued")
    second, authority = _attempt(migrated_url, authority_role_dsns, case, 2)
    assert _fail(authority_role_dsns, second, authority, _CONFLICT) == ("applied", "failed")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state, attempt, max_attempts, error_category, failure_context "
            "FROM jobs WHERE id = %s",
            (case.job_id,),
        ).fetchone() == ("failed", 2, 3, "infrastructure_failure", _CONFLICT)
        assert conn.execute(
            "SELECT array_agg(outcome ORDER BY job_attempt), "
            "array_agg(failure_context ORDER BY job_attempt) "
            "FROM external_boot_authority_audit WHERE job_id = %s",
            (case.job_id,),
        ).fetchone() == (["result_requeued", "result_failed"], [_CONFLICT, _CONFLICT])


@pytest.mark.parametrize("variant", ["no-reason", "phase-differs", "older-budget"])
def test_0170_non_identical_failures_still_requeue(
    migrated_url: str, authority_role_dsns: _RoleDsns, variant: str
) -> None:
    contexts = {
        "no-reason": ({"phase": "commit"}, {"phase": "commit"}),
        "phase-differs": ({**_CONFLICT, "phase": "provider-call"}, _CONFLICT),
        "older-budget": (_CONFLICT, _CONFLICT),
    }[variant]
    case = _teardown_case(migrated_url, variant[0])
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)
    assert _fail(authority_role_dsns, first, authority, contexts[0]) == ("applied", "queued")
    if variant == "older-budget":
        with psycopg.connect(migrated_url) as seed:
            seed.execute(
                "UPDATE jobs SET created_at = clock_timestamp() + interval '1 second' "
                "WHERE id = %s",
                (case.job_id,),
            )
    second, authority = _attempt(migrated_url, authority_role_dsns, case, 2)
    assert _fail(authority_role_dsns, second, authority, contexts[1]) == ("applied", "queued")


def test_0170_terminal_repeat_leaves_reservation_credit(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _teardown_case(migrated_url, "c")

    def credit() -> tuple[object, ...]:
        with psycopg.connect(migrated_url) as conn:
            return (
                conn.execute(
                    "SELECT state, reserved_bytes FROM external_boot_reservations "
                    "WHERE activation_id = %s",
                    (case.activation_id,),
                ).fetchall(),
                conn.execute(
                    "SELECT count(*) FROM external_boot_reservation_releases "
                    "WHERE activation_id = %s",
                    (case.activation_id,),
                ).fetchone(),
            )

    before = credit()
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)
    assert _fail(authority_role_dsns, first, authority, _CONFLICT) == ("applied", "queued")
    second, authority = _attempt(migrated_url, authority_role_dsns, case, 2)
    assert _fail(authority_role_dsns, second, authority, _CONFLICT) == ("applied", "failed")
    assert credit() == before


@pytest.mark.parametrize(
    ("reason", "category"),
    [("other", "infrastructure_failure"), ("provider-conflict", "boot_timeout")],
)
def test_0170_refuses_an_unknown_or_miscategorized_reason(
    migrated_url: str, authority_role_dsns: _RoleDsns, reason: str, category: str
) -> None:
    case = _teardown_case(migrated_url, "r")
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _fail(
            authority_role_dsns,
            first,
            authority,
            {"phase": "commit", "authority_reason": reason},
            category,
        )
```

Notes for the implementer:

- The seeded teardown case has a `failed` System and a `recovery_failed` activation, which the
  teardown `fail` precondition admits (migration 0160). If attempt 1 returns `superseded`, read
  `_seed_case` in `tests/db/external_boot_authority_support.py` and seed the way
  `test_0160_teardown_failure_leaves_reservation_credit` does (`_set_real_state`), because the
  repeat logic does not depend on the System state.
- `_seed_case` takes a `worker_suffix`. Each test above uses a different suffix, so the cases do
  not collide inside one database.

## Task 2: the worker carries the mark

Files: modify `src/kdive/jobs/authority_sender.py`, `src/kdive/jobs/models.py`,
`src/kdive/jobs/handlers/external_boot/runner.py`,
`tests/jobs/test_external_boot_authority_client.py`,
`tests/jobs/test_external_boot_authority_models.py`, and
`tests/jobs/handlers/external_boot/test_runner.py`.

Interfaces: consumes Task 1's SQL contract. Produces
`CategorizedError.details["authority_reason"] == "provider-conflict"` from
`kdive.jobs.authority_sender._failure`, and
`ExternalBootAuthorityFailureContext.authority_reason: Literal["provider-conflict"] | None`.

Verification:

- Mode: focused-test — sender mark. `test_sender_marks_only_provider_conflict` in
  `tests/jobs/test_external_boot_authority_client.py` is parametrized over `provider-conflict`,
  `provider-failure`, and `journal-conflict`. A stub transport returns the canonical error frame;
  `await sender.health(deadline=1.0)` raises a `CategorizedError` whose details equal
  `{"authority_reason": "provider-conflict"}` only for `provider-conflict`, and `{}` otherwise.
  Red: the details are `{}`. Green:
  `uv run python -m pytest tests/jobs/test_external_boot_authority_client.py -q`.
- Mode: focused-test — model validator. In `tests/jobs/test_external_boot_authority_models.py`,
  a `fail` result with `authority_reason` and category `boot_timeout` raises `ValidationError`,
  and with `infrastructure_failure` it validates. Red: the field is unknown (closed model).
- Mode: focused-test — runner carry and end to end.
  `test_bound_failure_carries_provider_conflict_and_repeat_ends_the_job` in `test_runner.py`
  makes `call_port` raise `_failure("provider-conflict")`. The bound result's
  `failure_context.authority_reason` is `"provider-conflict"` with `phase == "provider-call"`.
  It commits through `queue.fail_external_boot` and returns a queued job. A second identical
  attempt then ends `failed`. A control case raises `CategorizedError("x",
  category=INFRASTRUCTURE_FAILURE)` without details and gets `authority_reason is None`. Red:
  the context has no `authority_reason`.

Steps:

1. Write the three tests and run them. Expect red as described.
2. `authority_sender._failure`: after the `remote-module-refused` branch, add
   ```python
   elif reason == "provider-conflict":
       details = {"authority_reason": reason}
   ```
3. `models.py`: on `ExternalBootAuthorityFailureContext`, add
   `authority_reason: Literal["provider-conflict"] | None = None`. On `_FailureResult`, add
   ```python
   @model_validator(mode="after")
   def _authority_reason_is_infrastructure(self) -> _FailureResult:
       if (
           self.failure_context.authority_reason is not None
           and self.error_category is not ErrorCategory.INFRASTRUCTURE_FAILURE
       ):
           raise ValueError("authority reason requires infrastructure failure")
       return self
   ```
4. `runner._bound_failure`: after `failure_context: dict[str, object] = {"phase": phase}`, add
   ```python
   if (
       category is ErrorCategory.INFRASTRUCTURE_FAILURE
       and isinstance(exc, CategorizedError)
       and exc.details.get("authority_reason") == "provider-conflict"
   ):
       failure_context["authority_reason"] = "provider-conflict"
   ```
5. Run the three test files. Expect them all to pass. Run
   `uv run python -m pytest tests/jobs/handlers/external_boot tests/jobs/test_worker.py -q` and
   expect it to pass.
6. `just lint && just type`, then commit `feat(jobs): mark a provider-conflict failure for the repeat check (#2901)`.

## Completion

- Walk S1–S5 against the tests above. Run `git fetch origin main && just records`.
- The branch review, the security pass, and `just ci` follow under `$quest`.
- Deferrals: none.
