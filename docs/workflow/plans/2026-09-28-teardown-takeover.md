# Teardown takeover implementation plan (#2884)

**Goal:** settle an interrupted authority teardown through supported recovery: a teardown
takeover anchors over its own unresolved head, nothing else allocates over it, a failed teardown
job can be re-run, and the teardown proof binds its own generation.

**Architecture:** one migration (0161) patches two SQL functions by guarded text replacement; two
generator filters in `ExternalBootAuthorityService`; one branch in `_enqueue_authority_teardown`.
Spec: `docs/workflow/specs/2026-09-28-teardown-takeover-design.md`.

**Tech stack:** Python 3.14, psycopg 3, PL/pgSQL, pytest with disposable Postgres.

Expected implementation size: 420–560 changed lines (M) — one ~70-line migration, ~15 service
and admin lines, and ~350 lines across four test files.

## Global Constraints

- Migration number is exactly `0161`; it must be strictly above `origin/main`'s highest
  (`just migration-order-check`). Merged migrations are immutable (`just schema-guard`).
- Guarded replace: each target occurs exactly once and its replacement is absent, or the
  migration raises (0160's count idiom).
- No new ADR number; amend ADR-0620 with one dated `### Amendment` subsection.
- Ruff line length 100; `ty` whole tree; prose avoids "critical", "robust", "comprehensive",
  "elegant". Commits are conventional, one logical change each, and reference #2884.
- Guardrails: `just lint`, `just type`, `just records`, `just schema-guard`,
  `just migration-order-check`, focused `just test-verbose <paths>`.

## File map

| File | Change | Criterion |
|---|---|---|
| `src/kdive/db/schema/0161_external_boot_teardown_takeover.sql` | new | 1, 2 |
| `src/kdive/providers/external_boot_authority/service.py` | `_recovery_observation`, `_execute_teardown` filters | 5 |
| `src/kdive/mcp/tools/lifecycle/systems/admin.py` | `_enqueue_authority_teardown` recycle | 3 |
| `tests/db/test_migration_0161_teardown_takeover.py` | new: head, fence, service, recycle/credit | 1–5 |
| `tests/providers/local_libvirt/test_external_boot.py` | observation idempotency | 5 |
| `tests/mcp/lifecycle/test_systems_tools.py` | recycle behavior | 3 |
| `docs/adr/0620-authority-owned-system-teardown.md` | amendment | 6 |

## Task 1 — Takeover exemption (migration 0161, part 1)

**Verification:**
- Contract: a takeover `watermark-installed` with the same operation identity and a different
  attempt id anchors over a teardown head at each of `admitted`, `mutation-started`,
  `provider-returned`, `observed`, and sets `suspended_operation` to that head's operation.
  Mode: focused-test. `test_teardown_takeover_watermark_anchors_over_unresolved_head[<phase>]`;
  red on main: `conflict`. Green: `uv run python -m pytest
  tests/db/test_migration_0161_teardown_takeover.py -q -k watermark`.
- Contract: an operation-phase record with a changed attempt id is still `conflict`.
  Mode: focused-test. `test_operation_phase_record_with_changed_attempt_is_still_conflict`;
  passes before and after (guards the clause stays).
- Contract: the migration refuses to install when the target is absent or duplicated.
  Mode: task-test-not-applicable — the guard runs once at migration time over a function body
  this repository owns; a fault test would need a second divergent schema history.

Steps:
1. Write the test file with helpers imported from
   `tests.db.test_external_boot_authority_journal_migration` (`_record`, `_payload`,
   `_advance_raw`, `_head`, `_promote`, `_DIGEST`) and `tests.db.external_boot_authority_support`
   (`_seed_case`, `_allocate`, `_RoleDsns`). A helper `_drive_to(case, authority, phase)` runs
   watermark (1) → acknowledgement (2) → `_promote` → admitted (3) → … up to `phase`, with the
   teardown null identity pair (`expected_source_identity=None`, `intended_target_identity=None`,
   `recovery_objects=()`) and an `observation` on `observed`, returning the last record.
   `_reclaim(migrated_url, case)` bumps `jobs.attempt` and returns `replace(case, attempt=…)`,
   then `_allocate` gives the successor. The successor watermark is
   `_record(case, successor, n + 1, record_digest(head), WATERMARK_INSTALLED,
   attempt_id=successor.authority_id)`.
2. Run it: expect four `conflict` failures.
3. Create the migration:

```sql
-- Take over an interrupted System teardown (#2884, ADR-0620 amendment).
DO $$
DECLARE
    v_definition text;
    v_old constant text := $old$       AND v_head.operation_identity = p_record->>'operation_identity'
       AND (p_record->>'attempt_id' IS DISTINCT FROM v_head.head_record->>'attempt_id'$old$;
    v_new constant text := $new$       AND v_head.operation_identity = p_record->>'operation_identity'
       AND v_phase NOT IN ('watermark-installed', 'takeover-superseded', 'takeover-acknowledged')
       AND (p_record->>'attempt_id' IS DISTINCT FROM v_head.head_record->>'attempt_id'$new$;
BEGIN
    SELECT pg_get_functiondef(
        'public.advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)'::regprocedure
    ) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
       OR position(v_new IN v_definition) <> 0 THEN
        RAISE EXCEPTION 'external boot journal same-operation clause shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;
```

4. Rerun: all pass. Commit
   `fix(authority): let a takeover anchor over its own unresolved operation`.

## Task 2 — Allocation fence (migration 0161, part 2)

**Verification:**
- Contract: an `activate` allocation returns `superseded` while a teardown authority is
  `allocating`, while it is `current`, while the head is an unresolved teardown, and while the
  head carries a suspended teardown; a teardown allocation returns `allocated` in each case; an
  activate allocation after the teardown head is `terminal` with no live teardown authority is
  not refused by the fence. Mode: focused-test.
  `test_activate_allocation_is_fenced_by_teardown[<case>]`; red on main for `allocating`,
  `unresolved-head`, `suspended`. Green: `-k fenced`.

Steps:
1. Seed with `_seed_case(conn, purpose="activate")`, then insert a `teardown` job for the same
   activation with a teardown marker (copy of the activate marker with `purpose`/`operation`
   `teardown` and a fresh `operation_identity`), and allocate it. Build each state with the
   Task 1 helpers; the head-only states mark the teardown authority `superseded` with an admin
   `UPDATE` so only the head can refuse. The activate call reads the raw
   `allocate_external_boot_authority` status row.
2. Run: expect three failures.
3. Append the second block to 0161:

```sql
DO $$
DECLARE
    v_definition text;
    v_old constant text := $old$       OR (p_purpose <> 'teardown' AND EXISTS (
           SELECT 1 FROM public.external_boot_authorities AS teardown_authority
           WHERE teardown_authority.system_id = p_system_id
             AND teardown_authority.purpose = 'teardown'
             AND teardown_authority.state = 'current'
       )) THEN$old$;
    v_new constant text := $new$       OR (p_purpose <> 'teardown' AND (EXISTS (
           SELECT 1 FROM public.external_boot_authorities AS teardown_authority
           WHERE teardown_authority.system_id = p_system_id
             AND teardown_authority.purpose = 'teardown'
             AND teardown_authority.state IN ('allocating', 'current')
       ) OR EXISTS (
           SELECT 1 FROM public.external_boot_authority_journal_heads AS teardown_head
           WHERE teardown_head.system_id = p_system_id
             AND (teardown_head.suspended_operation->>'purpose' = 'teardown'
                  OR (teardown_head.phase IN (
                          'admitted', 'mutation-started', 'provider-returned', 'observed'
                      )
                      AND teardown_head.head_record->>'purpose' = 'teardown'))
       ))) THEN$new$;
BEGIN
    SELECT pg_get_functiondef((
        'public.allocate_external_boot_authority('
        || 'bytea,uuid,integer,uuid,uuid,uuid,text,text,text,text,text)'
    )::regprocedure) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
       OR position(v_new IN v_definition) <> 0 THEN
        RAISE EXCEPTION 'external boot teardown allocation fence shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;
```

4. Rerun green. Commit
   `fix(authority): keep other purposes off an unresolved teardown`.

## Task 3 — Proof context binds its own generation (service)

**Verification:**
- Contract: across three teardown generations sharing one operation identity (gen 1 interrupted
  at `mutation-started`, gen 2 recovers it and is interrupted at its own `mutation-started`,
  gen 3 recovers gen 2 and completes), gen 3's recovery observes with gen 2's
  `mutation-started` context and `execute_teardown` observes with gen 3's. Mode: focused-test.
  `test_teardown_takeover_recovers_and_proves_each_generation_through_real_cas` in
  `tests/db/test_migration_0161_teardown_takeover.py`; red on main: the recorded
  observation contexts name gen 1's sequence. Green: `-k each_generation`.

Steps:
1. The test drives `ExternalBootAuthorityService` over `_database_repository(...)` with a
   `_TeardownAdapter` (from `tests.providers.external_boot_authority.test_service_teardown`)
   subclass that raises `RuntimeError` from `execute_system_teardown` while `interrupt` is set
   and records `(kind, context.journal_sequence)` for every execute/observe call. The case is a
   `teardown` seed made ready with `_make_ready_prepared` and a ready reservation (from
   `tests.db.test_migration_0147_external_boot_system_teardown`). Each generation: `_reclaim`,
   `_allocate`, `acknowledge_takeover`, `execute_teardown` with `attempt_id =
   uuid5(NAMESPACE_URL, case.operation_identity)`.
2. Run: expect the context assertions to fail (gen 1's sequence).
3. In `_recovery_observation`, add `and item.authority_id == record.authority_id and
   item.generation == record.generation` to the `started` generator. In `_execute_teardown`, add
   `and record.authority_id == request.authority_id and record.generation ==
   request.generation` to `operation_records`.
4. Rerun green. Commit `fix(authority): bind teardown proof to its own generation`.

## Task 4 — Observation over a partial predecessor (local provider)

**Verification:**
- Contract: for a retained intent at each `SystemTeardownPhase` before `complete`,
  `observe_system_teardown` with that intent's own anchor returns identical facts twice, makes no
  session mutation, and leaves the recovery root byte-identical. Mode: focused-test.
  `test_system_teardown_observation_is_repeatable_over_a_partial_predecessor[<phase>]`; expected
  to pass on main (it pins behavior the settle relies on). Green: `-k partial_predecessor`.

Steps: write the parametrized test with `_system_teardown_io`, `_teardown_intent`, and
`RecoveryMetadataStore.begin_system_teardown` / `record_system_teardown_phase`; commit
`test(local-libvirt): pin teardown observation over a partial predecessor`.

## Task 5 — Recycle a failed authority teardown job

**Verification:**
- Contract: a second `systems.teardown` after the authority teardown job is `failed` with an
  identical payload returns `queued` on the same job id with `attempt = 0`. Mode: focused-test.
  `test_teardown_recycles_failed_authority_job_with_identical_marker`; red on main: status
  `failed`. Green: `-k recycles_failed_authority`.
- Contract: `running`, `succeeded`, `canceled`, and a `failed` job whose marker differs are
  returned unchanged. Mode: focused-test.
  `test_teardown_does_not_recycle_authority_job[<case>]`; passes before and after.
- Contract: after a recycle, a new attempt-1 authority allocates beside the superseded old
  attempt-1 row; the old row cannot finalize; the new one finalizes and credits the ready
  reservation once, and a repeated finalize stays `applied` with one release row. The failed
  attempt's authority is still `current` when a different incarnation claims the recycled job. Mode:
  focused-test. `test_recycled_teardown_job_credits_once` in the 0161 test file (uses real
  `queue.enqueue` with `JobRecyclePolicy.TERMINAL` over an async connection, and the 0147
  `_proof`/`_current` helpers). Passes on main (the fences already hold); it pins them.

Steps:
1. Write the tests; run; the first fails.
2. In `_enqueue_authority_teardown`, after the marker check, return the prior envelope unless
   `prior.state is JobState.FAILED`; after `build_external_boot_payload`, if `prior` is not
   `None` and the new payload's `external_boot_authority_v1` marker differs from the prior's,
   return the prior envelope; pass
   `recycle=queue.JobRecyclePolicy.TERMINAL if prior is not None else
   queue.JobRecyclePolicy.NEVER` to `queue.enqueue`. Comment: the System lock is held and only a
   `failed` row reaches here, so `TERMINAL` resets exactly that row.
3. Rerun green. Commit `fix(systems): re-run a failed authority teardown job`.

## ADR amendment

ADR-0620 `### Amendment (2026-09-28): an unresolved teardown owns the System (#2884)`, already
written with the design: the allocation fence, the failed-job recycle, the takeover exemption
from ADR-0584's same-operation rule, and generation-bound proof context.

## Deferrals

None at plan time.
