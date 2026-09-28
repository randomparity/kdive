# Teardown failure commit plan (#2881)

Goal: a teardown failure from the current attempt commits as fail or requeue for a System in any
admitted state, and the worker log separates a reclaim from a refused commit on a still-running attempt.

Architecture: migration 0160 edits `commit_external_boot_authority_result` by exact text
replacement (as 0147 does); the worker gains an advisory read after a `superseded` commit. Spec:
[2026-09-28-teardown-fail-commit-design.md](../specs/2026-09-28-teardown-fail-commit-design.md).

Tech stack: Python 3.14, psycopg 3, PostgreSQL migrations under `src/kdive/db/schema/`, pytest.

Expected implementation size: 220–290 changed lines (M) — one migration (~50), one queue helper
and worker branch (~25), the DB test module (~160), worker tests (~50), migration-list pins (~10).

## Global Constraints

- Migration number is exactly `0160` (campaign-assigned). Applied migrations are byte-immutable
  (`just schema-guard`); only add the new file.
- Do not touch `src/kdive/providers/**` (#2880's scope) or `tests/live_vm/**`.
- No binding-fence predicate in the commit function changes.
- The reclaim log line `external boot job %s was reclaimed; result dropped` stays byte-identical.
- Guardrails: `just lint`, `just type`, `just records`, `just schema-guard`,
  `just migration-order-check`, focused `just test-verbose <paths>`.

## File map

- Create `src/kdive/db/schema/0160_external_boot_teardown_failure_commit.sql` — owns the widened
  teardown fail precondition and the teardown Run-write exclusion.
- Create `tests/db/test_migration_0160_external_boot_teardown_failure_commit.py`.
- Modify `tests/db/test_migrate.py`, `tests/db/test_migration_0091_system_object_sweep_cursors.py`,
  `tests/db/test_migration_0102_build_gc_cursors.py`,
  `tests/db/test_migration_0115_capture_reap_state.py` — append `0160` to the pinned tails.
- Modify `src/kdive/jobs/queue.py` — add `external_boot_attempt_is_running`.
- Modify `src/kdive/jobs/worker.py` — `_commit_external_result` superseded branch.
- Modify `tests/jobs/test_external_boot_authority_models.py` — diagnostic tests.

## Task 1 — Commit teardown failures in the System's real state

Interfaces: consumes `_seed_case`, `_allocate`, `_RoleDsns` from
`tests/db/external_boot_authority_support.py` and the acknowledgement/commit SQL functions;
provides nothing to Task 2.

Verification:
- Contract: non-terminal teardown `fail` at System `ready`, activation `prepared`/`activating`
  applies and requeues. Mode: focused-test —
  `test_0160_commits_teardown_failure_for_nonfailed_system[prepared|activating]`; red before the
  migration file exists: status `superseded`; green:
  `just test-verbose tests/db/test_migration_0160_external_boot_teardown_failure_commit.py`.
- Contract: terminal teardown `fail` fails the job and leaves a `running` Run unchanged. Mode:
  focused-test — `test_0160_terminal_teardown_failure_leaves_the_run`; red before the migration
  (`superseded`).
- Contract: a newer activation, or a reclaim-bumped job attempt, still supersedes (criterion 2).
  Mode: focused-test — `test_0160_teardown_failure_is_superseded[newer_activation|reclaimed]`;
  passes before and after (the guard it protects); fault check: drop the `EXISTS` clause from
  `v_new` and observe the `newer_activation` case red.
- Contract: a requeued failure and its retried failure write no reservation or release row
  (criterion 4). Mode: focused-test — `test_0160_teardown_failure_leaves_reservation_credit`;
  red before the migration (first commit `superseded`).
- Contract: `queue.external_boot_attempt_is_running` reads the attempt as the worker role. Mode:
  focused-test — `test_attempt_is_running_reads_the_job_attempt` (same module; Task 2 adds the
  helper, so this case is written in Task 2 step 1); red: `AttributeError` until the helper
  exists.
- Contract: each replacement target occurs exactly once. Mode: focused-test —
  `test_0160_patch_targets_exist_once` builds a schema through 0159 (the
  `_apply_through` helper in `tests/db/external_boot_authority_support.py`) and asserts each
  `v_old` occurs once in `pg_get_functiondef`.

Steps:
1. Write the test module. Seed with `_seed_case(seed, purpose="teardown")`, set System `ready`,
   Run `succeeded` (or `running` for the terminal case), and activation `prepared` or
   `activating` with `recovery_point` (copy `_make_ready_prepared` from the 0147 test). Allocate
   as `kdive_worker`, mark the authority current and insert its acknowledgement as `_current` in
   the 0147 test does (sequence 1, digest `sha256:b…`), then call
   `commit_external_boot_authority_result` as `kdive_worker` with result
   `{"schema": "external-boot-authority-result-v1", "operation": "fail", "error_category":
   "infrastructure_failure", "failure_context": {"phase": "provider-call"}, "terminal": false}`
   and `admitted_operation='teardown'`.
2. Run the focused command; expect every applied-path test red with `('superseded', None)`.
3. Write the migration: a `DO $$` block reading
   `pg_get_functiondef('public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure)`,
   replacing
   ```
              OR (p_purpose = 'teardown' AND (
                  v_system.state IS DISTINCT FROM 'failed'
                  OR v_activation.state NOT IN ('recovery_conflict', 'recovery_failed')
              ))
          )) THEN
   ```
   with the 0147 allocation state lists and newer-activation `EXISTS`, and replacing
   the terminal-fail Run update, anchored on its unique first line (the two 0128 copies use
   `'stale_handle'`):
   ```
               SET state = 'failed', failure_category = p_result ->> 'error_category'
               WHERE id = p_run_id AND state IN ('created', 'running');
   ```
   with the same text ending `state IN ('created', 'running')` then a new line
   `              AND p_purpose <> 'teardown';`. For each target,
   `RAISE EXCEPTION` unless
   `(length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) = 1`,
   then `EXECUTE`.
4. Append `0160` to the four pinned migration tails; run the focused command plus
   `just test-verbose tests/db/test_migrate.py`; expect green.
5. Commit `fix: commit teardown failures for Systems in their real state`.

## Task 2 — Name the losing reason for a superseded external-boot commit

Interfaces: `async def external_boot_attempt_is_running(conn: AsyncConnection, job: Job) -> bool`
in `src/kdive/jobs/queue.py`, executing
`SELECT EXISTS (SELECT 1 FROM jobs WHERE id = %s AND attempt = %s AND state = 'running')`.

Verification:
- Contract: `SUPERSEDED` while the attempt is still running logs `is still running but its commit
  was refused`. Mode: focused-test —
  `test_worker_names_a_refused_commit_for_a_running_attempt`; red: the reclaim line is logged.
- Contract: `SUPERSEDED` after a reclaim keeps the exact reclaim line. Mode: focused-test —
  `test_worker_keeps_the_reclaim_line_after_a_reclaim`.
- Contract: a raising read logs `attempt state unreadable` and returns `False`. Mode:
  focused-test — `test_worker_survives_an_unreadable_attempt_state`.
- Green command: `just test-verbose tests/jobs/test_external_boot_authority_models.py
  tests/db/test_migration_0160_external_boot_teardown_failure_commit.py`.

Steps:
1. Write both tests with `monkeypatch.setattr(queue, "fail_external_boot",
   AsyncMock(return_value=queue.ExternalBootCommitStatus.SUPERSEDED))` and
   `queue.external_boot_attempt_is_running` patched to `True`/`False`/raising `OSError`,
   asserting `caplog` text; add the Task 1 module's DB case
   `test_attempt_is_running_reads_the_job_attempt` (running at attempt → `True`; attempt bumped →
   `False`; state `queued` → `False`), run on a `kdive_worker` async connection.
   Patch the helper in existing `SUPERSEDED` worker tests that reach it.
2. Run; expect the first red.
3. Implement: on `SUPERSEDED`, open a pool connection, call the helper, log the reclaim line when
   `False`, else the refused-commit line with `job.id` and `job.attempt`; wrap the read in
   `try/except Exception` logging the unreadable line with `exc_info=True`; return `False` in
   every case.
4. Run the green command, `just lint`, `just type`; commit
   `fix: separate reclaim from a refused commit in worker logs`.
