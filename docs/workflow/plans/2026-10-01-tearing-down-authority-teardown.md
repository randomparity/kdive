# The authority teardown finishes a `tearing_down` System (#3026) — implementation plan

Goal: `systems.teardown` finishes a `tearing_down` System with external-boot history through the
authority teardown while its Allocation is `active`. The stalled lane logs each skip once.

Spec: [2026-10-01-tearing-down-authority-teardown-design.md](../specs/2026-10-01-tearing-down-authority-teardown-design.md).
Decision: ADR-0620 amendment (2026-10-01), on the branch.

Architecture: forward-only migration `0168` adds `'tearing_down'` to the purpose-`teardown`
System-state list in three SQL functions. The worker precondition admits the same state. The
reconciler's warned set becomes a `system_id -> cause` map, pruned to each pass's candidates.

Tech stack: Postgres plpgsql, Python 3.14, psycopg 3, pytest.

Expected implementation size: 260–340 changed lines (M) — from the file map: migration ~30,
worker ~1, reconciler ~45, new 0168 test ~90, 0160 test arm ~15, integration arm ~45,
reconciler tests ~40, runbook delta ~60.

## Global Constraints

- Migration `src/kdive/db/schema/0168_tearing_down_authority_teardown.sql`. Applied migrations
  are byte-immutable (ADR-0015).
- Change only the purpose-`teardown` System-state list. The allocator's `active`-Allocation fence
  is #2992's.
- ADR-0620 is append-only; its amendment is already written. Edit it only if the build
  contradicts it.
- Guardrails: `just lint`, `just type`, focused `just test-verbose <paths>`, `just docs-links`,
  `just docs-paths`, `git fetch origin main && just records`. The push hook runs `just ci`; run
  it as `just ci > <file> 2>&1 < /dev/null`.
- Commit the implementation before any controlled-fault arm, because the revert step restores
  to HEAD.

## Task 1: migration 0168 and the worker precondition

Files:
- Create `src/kdive/db/schema/0168_tearing_down_authority_teardown.sql` and
  `tests/db/test_migration_0168_tearing_down_authority_teardown.py`.
- Modify `src/kdive/jobs/handlers/external_boot/lifecycle.py` (`_teardown_prerequisites`),
  `tests/db/test_migration_0160_external_boot_teardown_failure_commit.py`, and
  `tests/integration/test_external_boot_unrouted_teardown.py`.

Interfaces (existing at `3ed5b5c0e`, confirmed by reading the files):
- `tests/db/external_boot_authority_support.py`: `_ALLOCATE_SIGNATURE`, `_COMMIT_SIGNATURE`,
  `_PLAN`, `_AuthorityCase`, `_apply_through(conn, version)`, and
  `_allocate(worker, case) -> _Allocated` (asserts `allocated`).
- `tests/db/external_boot_journal_support.py`: `_ready_teardown_case(url, suffix)` (a `ready`
  System, a `prepared` activation, a `ready` reservation), `_proof(case, disposition)`,
  `_make_current(conn, case, authority, proof, sequence) -> str`, and
  `_finalize(role_dsns, case, authority, proof, sequence, digest) -> str`.
- The 0160 test: `_admitted(url, role_dsns, activation)` and `_commit(worker, case, authority)`.
- The integration file: `_boot`, `_teardown`, `_worker`, `_OUTCOME_SQL`, `_MARKER`, `fetch_one`.
- `kdive.jobs.queue.enqueue(conn, kind, payload, authorizing, dedup_key, *, recycle=...)`.

Verification:
- **Allocator and finalizer.** They admit `tearing_down`, take the edge `tearing_down->torn_down`,
  and credit once. Mode: focused-test, `test_0168_tearing_down_teardown_credits_once`.
  - Seed `_ready_teardown_case` and set the System to `tearing_down`.
  - Run `_allocate`, `_make_current` with `complete_ready`, then `_finalize` twice.
  - Assert: both finalizes return `applied`; the System is `torn_down`; there is one release row
    and zero reservations; the audit transitions for `tool = 'systems.teardown'` are
    `["tearing_down->torn_down"]`.
  - Red before 0168: `_allocate`'s `allocated` assertion fails, because the call returns
    `superseded`.
- **Failure commit.** It admits `tearing_down`. Mode: focused-test, a
  `system_state in ("ready", "tearing_down")` parameter on
  `test_0160_commits_teardown_failure_for_nonfailed_system`.
  - `_admitted` gains that argument and sets the System's state before `_allocate`.
  - Assert: `("applied", "queued")`, and the System keeps its seeded state.
  - Red before 0168: `_allocate` fails.
- **Expired Allocation.** It still allocates nothing. Mode: focused-test,
  `test_0168_expired_allocation_still_supersedes`.
  - Seed as for the allocator test, then set `allocations.state = 'expired'`.
  - Call the allocator directly.
  - Assert: the call returns `("superseded",)`, and the System has zero
    `external_boot_authorities` rows.
  - This test passes both before and after 0168. It turns red only if the `'active'` fence is
    widened.
- **Patch targets.** Each exists exactly once. Mode: focused-test,
  `test_0168_patch_targets_exist_once`.
  - Use `pg_conn` with `_apply_through(pg_conn, "0167")`.
  - Parse the migration's `$old$...$old$` literal.
  - Assert: each of the three function definitions contains the literal once and contains no
    `'tearing_down'`.
- **Worker and re-run.** The worker admits `tearing_down`, and a re-run `systems.teardown`
  recycles the exhausted marked row. Mode: focused-test, a `residue in ("none", "tearing_down")`
  parameter on `test_routed_teardown_completes_and_releases`. The `tearing_down` arm:
  1. After `cancel_job`, `queue.enqueue` an ordinary `TeardownPayload` row with key
     `{system}:teardown`, authorized for the System's project. Set that row to
     `failed`/`conflict` and the System to `tearing_down`.
  2. Call `_teardown`. Assert the same row id comes back `queued`.
  3. Set the row to `running` with `attempt = max_attempts`, the test `worker_id`, and
     `lease_expires_at = now() - interval '1 minute'`.
  4. Call `_teardown` again. Assert the same id is `queued`.
  5. Run the worker once. Assert the existing outcome, plus the audit transitions
     `["tearing_down->torn_down"]`.
  - Red before step 5 below: the worker refuses at the precondition, and the row stays
    `running` (`teardown_job == {"state": "running"}`).

Steps:

1. Write the three 0168 tests and the 0160 arm. Run `just test-verbose
   tests/db/test_migration_0168_tearing_down_authority_teardown.py
   tests/db/test_migration_0160_external_boot_teardown_failure_commit.py`. Expect:
   - `patch_targets` fails with `StopIteration`;
   - `credits_once` and the 0160 `tearing_down` arm fail on `_allocate`;
   - `expired_allocation_still_supersedes` passes.
2. Write the migration:

```sql
-- 0168_tearing_down_authority_teardown.sql — #3026, ADR-0620 amendment (2026-10-01).
-- Forward-only (ADR-0015). A pre-#2966 ordinary teardown can leave a System with external-boot
-- history in `tearing_down`, which only the authority teardown may finish. Admit that state in
-- the three purpose-`teardown` System-state lists: the allocator (0147), the success finalizer
-- (0147, 0149) and the failure commit (0160). Each definition carries the list's tail exactly
-- once. Every other fence, including the `active` Allocation check (#2992), is unchanged.
DO $$
DECLARE
    v_old constant text := $old$'paused', 'crashing', 'crashed', 'failed'$old$;
    v_new constant text := $new$'paused', 'crashing', 'crashed', 'failed', 'tearing_down'$new$;
    v_function regprocedure;
    v_definition text;
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,'
        'text,text,text)',
        'public.finalize_external_boot_authority_teardown(bytea,uuid,integer,uuid,bigint,bigint,'
        'text,bytea)',
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,'
        'uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'
    ]::regprocedure[] LOOP
        v_definition := pg_get_functiondef(v_function);
        IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
           OR position('''tearing_down''' IN v_definition) <> 0 THEN
            RAISE EXCEPTION 'external boot teardown System-state list changed in %', v_function;
        END IF;
        EXECUTE replace(v_definition, v_old, v_new);
    END LOOP;
END
$$;
```

3. Rerun step 1. Expect every test to pass.
4. Add the integration arm. Run `just test-verbose
   tests/integration/test_external_boot_unrouted_teardown.py`. Expect the `tearing_down` arm to
   fail with the row `running`.
5. In `_teardown_prerequisites`, add `"tearing_down",` after `"failed",` in `admitted_states`.
   Rerun step 4. Expect every test to pass.
6. Run `just lint` and `just type`. Commit
   `feat(external-boot): authority teardown admits tearing_down (#3026)`.
7. Controlled fault: delete `"tearing_down",` from the set and rerun step 4; observe red. Then
   restore it with `git checkout -- src/kdive/jobs/handlers/external_boot/lifecycle.py`.

## Task 2: stalled-lane visibility and pruning

Files: modify `src/kdive/reconciler/repairs/systems.py` and
`tests/reconciler/test_stalled_teardown_recovery.py`.

Interfaces (existing): `repair_stalled_tearing_down_systems(conn) -> int`; `Job.id`,
`Job.state: JobState`, and `Job.payload`; the test helpers `connect`, `run_repair`,
`seed_system`, `_seed_teardown_job`, and `_seed_completed_activation`.

Produces:
- `_warned_stalled_teardowns: dict[UUID, str]`, which replaces `_warned_stalled_teardown_history`.
- `_warn_stalled_teardown_once(system_id, cause, message, *args) -> None`, which replaces
  `_warn_stalled_teardown_history`.

Verification:
- **Marked prior row.** It warns once and names its exit. Mode: focused-test. Extend the
  parametrized `test_authority_marked_row_is_not_overwritten`, which runs for both marker keys:
  - clear the map, then run the repair twice;
  - assert one WARNING containing the System id;
  - assert the message contains `systems.teardown` for `external_boot_authority_v1` and
    `no supported exit` for `authority_system_v1`.
  - Red: no warning.
- **Pruning.** A System that leaves `tearing_down` is dropped from the map and warns again when
  it returns. Mode: focused-test, `test_warned_map_drops_a_system_that_leaves_tearing_down`:
  - seed a `tearing_down` System with a completed activation and a failed job;
  - run once and assert the key is present;
  - set the System to `torn_down`, run, and assert the key is absent;
  - set it back to `tearing_down`, run, and assert two WARNINGs in total.
  - Red: the key remains present.
- **History warning.** It still fires once. Mode: focused-test, the existing
  `test_external_boot_history_is_not_requeued_and_warns_once`, with its `.clear()` pointed at
  the new map.

Steps:

1. Write the tests. Run `just test-verbose tests/reconciler/test_stalled_teardown_recovery.py`.
   Expect the two new contracts to fail.
2. Replace the set and the helper:

```python
# System id -> the cause last warned about for a skipped stalled `tearing_down` (#3015, #3026).
# Pruned every pass to that pass's candidates, so a System that leaves `tearing_down` is dropped.
_warned_stalled_teardowns: dict[UUID, str] = {}
_STUCK_TEARING_DOWN_RUNBOOK = "docs/operating/runbooks/stuck-tearing-down-system.md"


def _warn_stalled_teardown_once(system_id: UUID, cause: str, message: str, *args: object) -> None:
    if _warned_stalled_teardowns.get(system_id) == cause:
        return
    _warned_stalled_teardowns[system_id] = cause
    _log.warning(message, system_id, *args)
```

3. Prune after `candidates` is read:

```python
    for gone in _warned_stalled_teardowns.keys() - set(candidates):
        del _warned_stalled_teardowns[gone]
```

4. Replace the marker `continue` with a warning, then `continue`. Choose the cause by marker key:
   - `EXTERNAL_BOOT_AUTHORITY_MARKER_KEY` gives cause `"authority_marked"` and the message
     `"reconciler: system %s is stuck in tearing_down behind authority-marked teardown job %s
     (%s); this lane never replaces it, so re-run systems.teardown (%s)"`.
   - Any other key gives cause `"authority_system_marked"` and the message `"... behind
     authority-System teardown job %s (%s); no supported exit exists for it (%s)"`.
   - The arguments are `existing.id`, `existing.state.value`, and `_STUCK_TEARING_DOWN_RUNBOOK`.

   Replace the history warning with
   `_warn_stalled_teardown_once(system_id, "external_boot_history", ...)`. Keep its text, but
   replace "needs operator recovery" with "run systems.teardown while its Allocation is active
   (%s)", passing the runbook path.
5. Docstring: replace "The skip logs one WARNING per System per process; the supported exit is
   tracked in #3026." with "Each skip, including a non-active authority-marked prior row, logs
   one WARNING per System and cause; the warned map is pruned to each pass's candidates
   (#3026)."
6. Rerun step 1 and expect a pass. Run `just lint` and `just type`. Commit
   `feat(reconciler): warn on a marked stalled teardown and prune the warned map (#3026)`.
7. Controlled fault: delete the prune loop and observe the prune test fail. Restore it with
   `git checkout -- src/kdive/reconciler/repairs/systems.py`.

## Task 3: runbook

Files: rewrite `docs/operating/runbooks/stuck-tearing-down-system.md`, and change its row in
`docs/operating/index.md`.

Verification:
- **Links and doc paths.** They resolve, including the path the reconciler logs. Mode:
  focused-test, `just docs-links` and `just docs-paths` exit 0.
- **ADR.** It stays valid and append-only. Mode: focused-test,
  `git fetch origin main && just records` exits 0, and
  `git diff origin/main -- docs/adr/0620-authority-owned-system-teardown.md` shows only added lines.
- **Runbook prose.** Mode: task-test-not-applicable — operator prose with no executable
  consumer.

Steps:

1. Write the runbook sections:
   - **Symptom:** the three WARNINGs.
   - **Confirm the state:** keep both queries.
   - **Recover**, while the Allocation is `active`:
     - Run `systems.teardown` (project `admin`) or `ops.force_teardown` (platform admin,
       `--force`, and a reason). Either replaces a failed unmarked row, or recycles an exhausted
       marked `running` row, and the authority teardown ends at `torn_down`.
     - If the lease is near expiry, run `allocations.renew` first; the extension is clamped to
       the lease maximum.
     - Then run `allocations.release` and `investigations.close`.
   - **Limits:**
     - A marked row that is `running` with attempts left resumes on its own.
     - After expiry there is no supported exit until #2992.
     - An authority-System marker with no activation has no supported exit.
     - Never write `systems.state` by hand.
2. Index row: "Ending a `tearing_down` System with external-boot history through the authority
   teardown".
3. Run the doc guardrails. Commit
   `docs(operating): runbook names the authority teardown exit (#3026)`.

## Spec coverage

| Spec item | Task |
|---|---|
| Design 1–3; Success (database) | Task 1, migration and 0160 tests |
| Design 2, 4; Success (end to end) | Task 1, integration arm |
| Design 5; Success (warnings) | Task 2 |
| Design 6 | Task 3 |
