# Settlement ends the stranded boot job — implementation plan

Goal: `settle_stranded_external_boot_activation` fails the stranded `runs.boot`'s exhausted,
lease-lapsed, ungranted `boot` job and its Run, audited, instead of refusing `job_live` (#3596).

Architecture: migration 0198 patches the stored 0194 function with three exact, asserted text
replacements (the 0150 pattern). No Python changes. Spec:
[2026-11-14-settle-ends-exhausted-boot-job-3596-design.md](../specs/2026-11-14-settle-ends-exhausted-boot-job-3596-design.md).

Tech stack: PostgreSQL PL/pgSQL migrations under `src/kdive/db/schema/`, pytest real-database
tests under `tests/db/` (`migrated_url`, `authority_role_dsns` fixtures).

Expected implementation size: 230–290 changed lines (M) — migration ~75, new test ~120, window
tests ~10, end-to-end test ~15, runbook ~10.

## Global Constraints

- Migration number is exactly `0198` (`src/kdive/db/schema/0198_settle_ends_exhausted_boot_job.sql`);
  forward-only (ADR-0015). A new migration adds `"0198"` / `("0198", "<file>")` after every `0197`
  entry in `tests/db/test_migrate.py` (four lists) and bumps each `migrations[-K:]` to `K+1` in
  `test_migrate.py`, `test_migration_0091_system_object_sweep_cursors.py`,
  `test_migration_0102_build_gc_cursors.py`, `test_migration_0115_capture_reap_state.py`.
- ADR-0816 is amended append-only (done in the design commit).
- Guardrails: `just test-verbose <paths>`, `just lint`, `just type`, `just records`,
  `just migration-order-check`, `just docs-links`; final owner is the pre-push `just ci` hook.
- 100-character lines; ruff format.

## File map

| File | Change | Owns |
|---|---|---|
| `src/kdive/db/schema/0198_settle_ends_exhausted_boot_job.sql` | create | the amended settle function |
| `tests/db/test_migration_0198_settle_ends_exhausted_boot_job.py` | create | its real-database contract |
| four window tests (above) | modify | migration registry |
| `tests/db/stranded_activation_support.py`, the 0197 test | modify | shared `_record_both` |
| `tests/db/test_stranded_recovery_end_to_end.py` | modify | tool-level arm without `jobs.cancel` |
| `docs/operating/runbooks/stranded-activation-recovery.md` | modify | operator procedure |

## Task 1: the migration and its contract

Verification:
- Contract "an exhausted, lapsed, ungranted boot job of the settled activation is failed with its
  Run and audited". Mode: focused-test. `test_settlement_fails_the_exhausted_job_and_its_run`;
  red before the migration: settle returns `job_live`. Green:
  `just test-verbose tests/db/test_migration_0198_settle_ends_exhausted_boot_job.py`.
- Contract "a held or claimable job still refuses `job_live`, and a later refusal ends no job".
  Mode: focused-test. `test_a_held_job_or_a_refusal_changes_nothing` (live lease, attempts
  remaining, queued, another activation, `evidence_stale`),
  `test_a_claimable_grant_keeps_the_job_live`, `test_a_locked_job_refuses_without_waiting`; same
  green command.
- Contract "migration registry". Mode: focused-test. Window tests; green:
  `just test-verbose tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py`.

Interfaces: consumes `_strand`, `_Stranded`, `_DOMAIN` (`tests/db/stranded_activation_support.py`),
`_RoleDsns` (`tests/db/external_boot_authority_support.py`), the 0194 SQL functions
`record_stranded_activation_authority_absence(uuid,text,text,bigint,text,text,text[])`,
`record_stranded_activation_worker_ownership(bytea,uuid,integer,uuid,text)`,
`settle_stranded_external_boot_activation(uuid,text,text,text,text)`, and the 0165
`has_acknowledged_external_boot_retry_proof(public.jobs)`.

Steps:

1. Move `_record_both` from `tests/db/test_migration_0197_settled_teardown_route_fence.py` into
   `tests/db/stranded_activation_support.py` unchanged with its `_call` helper (add `LiteralString` to
   the support module's `typing` import); the 0197 test imports `_call` and `_record_both` from
   there and drops its now-unused `_DOMAIN` import. Then write the test file:

```python
"""Real-Postgres proof that settlement ends the stranded boot job (#3596, ADR-0816)."""

from __future__ import annotations

from typing import Any, LiteralString, cast
from uuid import UUID

import psycopg
import pytest

from kdive.db import migrate
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.stranded_activation_support import _record_both, _strand, _Stranded

_SETTLE = "SELECT settle_stranded_external_boot_activation(%s,%s,%s,%s,%s)"
_SETTLE_ARGS = ("admin", "session", "operator-cli", "stranded by #3596")


def test_migration_0198_is_registered() -> None:
    assert "0198" in [item.version for item in migrate.discover_migrations()]


def _exhausted(migrated_url: str, role_dsns: _RoleDsns) -> _Stranded:
    """The live strand: both owners' evidence, and the boot job running past its last attempt."""
    stranded = _strand(migrated_url, role_dsns)
    _record_both(role_dsns, stranded)
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state = 'running', attempt = max_attempts, worker_id = %s, "
            "heartbeat_at = now() - interval '10 minutes', "
            "lease_expires_at = now() - interval '5 minutes' WHERE id = %s",
            (stranded.case.worker_id, stranded.case.job_id),
        )
        conn.execute("UPDATE runs SET state = 'running' WHERE id = %s", (stranded.case.run_id,))
    return stranded


def _settle(role_dsns: _RoleDsns, activation_id: UUID) -> str:
    with psycopg.connect(role_dsns("kdive_server"), autocommit=True) as conn:
        row = conn.execute(_SETTLE, (activation_id, *_SETTLE_ARGS)).fetchone()
    assert row is not None
    return row[0]


def _rows(migrated_url: str, stranded: _Stranded) -> tuple[Any, ...]:
    case = stranded.case
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT j.state, j.error_category, j.attempt, j.lease_expires_at, r.state, "
            "r.failure_category, (SELECT count(*) FROM platform_audit_log "
            "WHERE scope = 'failed_job:' || j.id::text) "
            "FROM jobs j, runs r WHERE j.id = %s AND r.id = %s",
            (case.job_id, case.run_id),
        ).fetchone()
    assert row is not None
    return row


def test_settlement_fails_the_exhausted_job_and_its_run(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _exhausted(migrated_url, authority_role_dsns)

    assert _settle(authority_role_dsns, stranded.case.activation_id) == "applied"

    ended = _rows(migrated_url, stranded)
    assert ended[:2] + ended[4:] == ("failed", "lease_expired", "failed", "lease_expired", 1)
    assert _settle(authority_role_dsns, stranded.case.activation_id) == "replayed"
    assert _rows(migrated_url, stranded)[-1] == 1


_UNCHANGED = {
    "live-lease": "UPDATE jobs SET lease_expires_at = now() + interval '1 hour' WHERE id = {job}",
    "attempts-remaining": "UPDATE jobs SET max_attempts = attempt + 1 WHERE id = {job}",
    "queued": "UPDATE jobs SET state = 'queued', worker_id = NULL, lease_expires_at = NULL, "
    "heartbeat_at = NULL WHERE id = {job}",
    "another-activation": "UPDATE jobs SET payload = jsonb_set(payload, "
    "'{{external_boot_authority_v1,activation_id}}', to_jsonb(gen_random_uuid()::text)) "
    "WHERE id = {job}",
    "later-refusal": "UPDATE external_boot_activation_settlements "
    "SET worker_recorded_at = now() - interval '2 hours' WHERE activation_id = {activation}",
}


@pytest.mark.parametrize("shape", sorted(_UNCHANGED))
def test_a_held_job_or_a_refusal_changes_nothing(
    migrated_url: str, authority_role_dsns: _RoleDsns, shape: str
) -> None:
    stranded = _exhausted(migrated_url, authority_role_dsns)
    case = stranded.case
    sql = _UNCHANGED[shape].format(job=f"'{case.job_id}'", activation=f"'{case.activation_id}'")
    expected = "evidence_stale" if shape == "later-refusal" else "job_live"
    with psycopg.connect(migrated_url) as conn:
        conn.execute(cast(LiteralString, sql))
    before = _rows(migrated_url, stranded)

    assert _settle(authority_role_dsns, case.activation_id) == expected

    assert _rows(migrated_url, stranded) == before


def test_a_claimable_grant_keeps_the_job_live(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """An unconsumed acknowledged-retry grant lets a worker claim the job again (0150)."""
    stranded = _exhausted(migrated_url, authority_role_dsns)
    # The predicate is private to definer functions; stand it in for one transaction, rolled
    # back, so no other test sees it. A superuser session passes the server-role check.
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION public.has_acknowledged_external_boot_retry_proof("
            "p_job public.jobs) RETURNS boolean LANGUAGE sql STABLE RETURNS NULL ON NULL INPUT "
            "SET search_path = '' AS 'SELECT true'"
        )
        row = conn.execute(_SETTLE, (stranded.case.activation_id, *_SETTLE_ARGS)).fetchone()
        conn.rollback()
    assert row == ("job_live",)
    assert _rows(migrated_url, stranded)[0] == "running"


def test_a_locked_job_refuses_without_waiting(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _exhausted(migrated_url, authority_role_dsns)
    with psycopg.connect(migrated_url) as holder:
        holder.execute("SELECT 1 FROM jobs WHERE id = %s FOR UPDATE", (stranded.case.job_id,))
        assert _settle(authority_role_dsns, stranded.case.activation_id) == "job_live"
        holder.rollback()
    assert _rows(migrated_url, stranded)[0] == "running"
```

2. Run `just test-verbose tests/db/test_migration_0198_settle_ends_exhausted_boot_job.py`.
   Expect `test_migration_0198_is_registered` and `test_settlement_fails_...` to fail
   (`job_live` != `applied`); the `job_live` cases already pass.
3. Write the migration:

```sql
-- 0198_settle_ends_exhausted_boot_job.sql — #3596, ADR-0816 amendment (2026-11-14).
-- Forward-only (ADR-0015). Settlement fails the stranded runs.boot's exhausted, lapsed, ungranted
-- boot job and its Run with lease_expired (the 0165 values), audited, instead of refusing job_live.
-- Exact, asserted replacements of the stored body (the 0150 pattern) keep signature, owner, grants.
DO $migration$
DECLARE
    v_function constant regprocedure :=
        'public.settle_stranded_external_boot_activation(uuid,text,text,text,text)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old text[] := ARRAY[
        $old$    v_reason text := regexp_replace(p_reason, '^\s+|\s+$', '', 'g');$old$,
        $old$    IF EXISTS (SELECT 1 FROM public.jobs AS job
               WHERE job.state IN ('queued', 'running')
                 AND job.payload #>> '{external_boot_authority_v1,system_id}'
                     = v_system.id::text) THEN
        RETURN 'job_live';
    END IF;$old$,
        $old$    UPDATE public.external_boot_authorities
    SET state = 'superseded', superseded_at = clock_timestamp()
    WHERE activation_id = p_activation_id AND state IN ('allocating', 'current');$old$
    ];
    v_new text[] := ARRAY[
        $new$    v_reason text := regexp_replace(p_reason, '^\s+|\s+$', '', 'g');
    v_exhausted uuid[];$new$,
        $new$    BEGIN
        SELECT coalesce(array_agg(exhausted.id ORDER BY exhausted.id), '{}') INTO v_exhausted
        FROM (
            SELECT job.id FROM public.jobs AS job
            WHERE job.kind = 'boot' AND job.state = 'running'
              AND job.attempt >= job.max_attempts
              AND job.lease_expires_at < clock_timestamp()
              AND job.payload #>> '{external_boot_authority_v1,system_id}' = v_system.id::text
              AND job.payload #>> '{external_boot_authority_v1,activation_id}'
                  = p_activation_id::text
              AND NOT public.has_acknowledged_external_boot_retry_proof(job)
            FOR UPDATE OF job NOWAIT
        ) AS exhausted;
    EXCEPTION WHEN lock_not_available THEN
        RETURN 'job_live';
    END;
    IF EXISTS (SELECT 1 FROM public.jobs AS job
               WHERE job.state IN ('queued', 'running')
                 AND job.payload #>> '{external_boot_authority_v1,system_id}'
                     = v_system.id::text
                 AND job.id <> ALL (v_exhausted)) THEN
        RETURN 'job_live';
    END IF;$new$,
        $new$    UPDATE public.jobs SET state = 'failed', error_category = 'lease_expired'
    WHERE id = ANY (v_exhausted);
    UPDATE public.runs SET state = 'failed', failure_category = 'lease_expired'
    WHERE id IN (SELECT (job.payload #>> '{external_boot_authority_v1,run_id}')::uuid
                 FROM public.jobs AS job WHERE job.id = ANY (v_exhausted))
      AND state IN ('created', 'running');
    INSERT INTO public.platform_audit_log (
        principal, agent_session, platform_role, tool, scope, args_digest, actor
    )
    SELECT p_principal, p_agent_session, 'platform_admin',
           'external_boot.settle_stranded_activation', 'failed_job:' || ended.id::text,
           encode(sha256(convert_to(public.canonical_external_boot_authority_json(
               jsonb_build_object('activation_id', p_activation_id::text,
                                  'job_id', ended.id::text, 'reason', v_reason)
           ), 'UTF8')), 'hex'),
           p_actor
    FROM unnest(v_exhausted) AS ended (id);
    UPDATE public.external_boot_authorities
    SET state = 'superseded', superseded_at = clock_timestamp()
    WHERE activation_id = p_activation_id AND state IN ('allocating', 'current');$new$
    ];
BEGIN
    FOR i IN 1 .. array_length(v_old, 1) LOOP
        IF (length(v_definition) - length(replace(v_definition, v_old[i], '')))
           <> length(v_old[i]) THEN
            RAISE EXCEPTION 'settlement function has an unexpected shape at replacement %', i;
        END IF;
        v_definition := replace(v_definition, v_old[i], v_new[i]);
    END LOOP;
    EXECUTE v_definition;
END
$migration$;
```

4. Run the step-2 command; expect every test to pass. Then add `"0198"` after each `"0197"` list
   entry and `("0198", "0198_settle_ends_exhausted_boot_job.sql")` after each `("0197", ...)`
   entry in the four window tests, bump each `migrations[-K:]` by one, and run the registry
   command; expect pass.
5. `just lint && just type && just migration-order-check`; commit
   `fix(db): settle ends the stranded exhausted boot job`.

Acceptance: every test above green; a refusal changes no job or Run; the migration fails loudly
if the 0194 body differs from the three expected fragments.

## Task 2: the tool-level arm and the runbook

Verification:
- Contract "the operator's tools settle the live strand with no `jobs.cancel`". Mode:
  focused-test. `test_an_exhausted_boot_job_ends_with_the_settlement` in
  `tests/db/test_stranded_recovery_end_to_end.py`; red on a tree without Task 1 (settle refuses
  `job_live`). Green: `just test-verbose tests/db/test_stranded_recovery_end_to_end.py`.
- Contract "runbook procedure". Mode: task-test-not-applicable — operator prose with no
  executable consumer; `just docs-links` covers its links.

Interfaces: consumes `_Lab`, `_server_pool` from the same test module (unchanged).

Steps:

1. In `test_an_exhausted_boot_job_refuses_until_it_is_canceled`, rename it
   `test_an_exhausted_boot_job_ends_with_the_settlement`, end its docstring's second paragraph
   with "so settlement fails it and compensates its Run, with no ``jobs.cancel`` (#3596).", and
   apply:

```diff
             "lease_expires_at = now() - interval '5 minutes', "
-            # The fixture's row predates the stored-Job contract that jobs.cancel reads.
-            "authorizing = authorizing || '{\"agent_session\": null}' WHERE id = %s",
+            "WHERE id = %s",
             (lab.case.worker_id, lab.case.job_id),
         )
+        conn.execute("UPDATE runs SET state = 'running' WHERE id = %s", (lab.case.run_id,))
@@
             await lab.observe(pool)
-            refused = await lab.recover(pool, "settle")
-            assert refused.data["reason"] == "job_live"
-            canceled = await cancel_job(pool, ctx(Role.CONTRIBUTOR), str(lab.case.job_id))
-            assert canceled.status == "canceled"
-            await lab.observe(pool)
             settled = await lab.recover(pool, "settle")
@@
     assert lab.activation_state() == "abandoned"
+    assert lab.row("SELECT state FROM jobs WHERE id = %s", lab.case.job_id) == "failed"
+    assert lab.row("SELECT state FROM runs WHERE id = %s", lab.case.run_id) == "failed"
```

   Remove the now-unused `cancel_job` import.
2. Runbook `docs/operating/runbooks/stranded-activation-recovery.md`: replace the Recover step 2
   paragraph beginning "The `runs.boot` that stranded the System" with: "The `runs.boot` that
   stranded the System usually leaves its `boot` job `running` after its last attempt.
   Settlement fails that job and its Run with `lease_expired`, and audits it, when its `attempt`
   has reached its `max_attempts`, its lease has lapsed and no worker can claim it again." Replace
   the `job_live` row's action with "An authority-marked job for the System is queued or running
   and a worker holds it or can still claim it, or the reconciler or a worker was updating an
   exhausted `boot` job at that moment. Wait for any live job to end, then `observe` again."
   In the `authority_objects_present` row replace "Nothing in kdive removes them yet (#3587),
   so" with "Nothing in kdive removes them, so".
3. Run the green command and `just docs-links`; commit
   `docs(runbook): drop the manual cancel from stranded recovery`.

## Task 3: live proof (orchestrator-gated)

Verification: Mode: task-test-not-applicable — environment proof, not a repository test.

Ask the orchestrator for the live slot and wait for `DECISION:`. On grant, deploy the committed
branch head to the granted lab, strand a fixed-worker `runs.boot` per the private lab notes, run
`observe` then `settle`, and record: `settled` with no `jobs.cancel`, the `boot` job `failed`
(`lease_expired`), the Run `failed`, one `failed_job:` audit row. Evidence stays private;
the PR states only the public-safe outcome.
