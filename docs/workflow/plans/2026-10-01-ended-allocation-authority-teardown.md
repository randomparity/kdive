# The authority teardown runs on a released or expired Allocation (#2992) — implementation plan

Goal: `allocate`, `acknowledge`, and `commit` admit purpose `teardown` whatever the Allocation
state, so a System with external-boot history on an expired Allocation reaches `torn_down`.

Spec: [2026-10-01-ended-allocation-authority-teardown-design.md](../specs/2026-10-01-ended-allocation-authority-teardown-design.md).

Architecture: forward-only migration `0169` rewrites one literal in three security-definer
functions with the 0168 `pg_get_functiondef` replace pattern. The DB suite proves both arms per
function; one integration arm proves the public route end to end. ADR-0584 and ADR-0620 get
dated amendments; the runbook loses its "no exit after expiry" limit.

Tech stack: Postgres plpgsql, Python 3.14, psycopg 3, pytest.

Expected implementation size: 230–300 changed lines (M) — from the file map: migration ~30,
0169 tests ~150, helper move ~5 net, 0168 test removal ~-40, window lists ~10, integration arm
~25, ADR amendments ~40, runbook ~-10.

## Global Constraints

- Migration `src/kdive/db/schema/0169_ended_allocation_authority_teardown.sql` (assigned 0169).
  Applied migrations are byte-immutable (ADR-0015); never edit 0122–0168.
- Only the literal `v_allocation.state <> 'active'` changes, and only for purpose `teardown`.
  `register_authority_system_ownership` (0149) is excluded.
- ADR-0584 and ADR-0620 are merged: append a dated `### Amendment` section only.
- Guardrails: `just lint`, `just type`, focused `just test-verbose <paths>`, `just docs-links`,
  `just docs-paths`, `git fetch origin main && just records`. The push hook runs `just ci`.
- Commit the implementation before any controlled-fault arm.

## Task 1: migration 0169 and its DB proofs

Files:
- Create `src/kdive/db/schema/0169_ended_allocation_authority_teardown.sql`,
  `tests/db/test_migration_0169_ended_allocation_authority_teardown.py`.
- Modify `tests/db/external_boot_authority_support.py` (receives `_prepare_purpose_state`, moved
  unchanged from `tests/db/test_external_boot_authority_migration.py`, which then imports it),
  `tests/db/test_migration_0168_tearing_down_authority_teardown.py` (delete
  `test_0168_expired_allocation_still_supersedes` and the imports only it used),
  and the migration-window lists: `tests/db/test_migrate.py` (four lists: add `"0169"` /
  `("0169", "0169_ended_allocation_authority_teardown.sql")` and `[-67:]` -> `[-68:]`),
  `tests/db/test_migration_0091_system_object_sweep_cursors.py`,
  `tests/db/test_migration_0102_build_gc_cursors.py`,
  `tests/db/test_migration_0115_capture_reap_state.py` (append the row, widen the slice by one).

Interfaces: consumes `_seed_case`, `_allocate`, `_apply_through`, `_AuthorityCase`, `_Allocated`,
`_PLAN`, `_JOURNAL`, `_QUIESCENCE`, the three `_*_SIGNATURE` constants
(`tests/db/external_boot_authority_support.py`); `_ready_teardown_case`, `_proof`,
`_make_current`, `_finalize` (`tests/db/external_boot_journal_support.py`); fixtures
`pg_conn`, `migrated_url`, `authority_role_dsns`.

Verification:
- Contract: purpose `teardown` passes the Allocation fence at all three functions on `released`
  and `expired`. Mode: focused-test. Cases `test_0169_teardown_completes_on_ended_allocation`
  and `test_0169_teardown_failure_commits_on_ended_allocation`; red before the migration file
  exists: allocate returns `superseded` (`_allocate` assertion fails). Green:
  `just test-verbose tests/db/test_migration_0169_ended_allocation_authority_teardown.py`.
- Contract: every non-teardown purpose (`activate`, `recover`, `resolve-conflict`, `release`)
  keeps the fence at all three functions. Mode: focused-test.
  `test_0169_other_purposes_keep_the_allocation_fence`; red under a controlled fault that drops
  `AND p_purpose <> 'teardown'` from the replacement: allocate returns `allocated`.
- Contract: the migration refuses a changed function shape. Mode: focused-test.
  `test_0169_patch_target_exists_once` asserts the literal occurs exactly once in each function
  migrated through 0168, and the replacement is absent.
- Contract: migration-window lists include 0169. Mode: focused-test. The four list tests fail
  red as soon as the file exists; green after the list edits.

Steps:

1. Move `_prepare_purpose_state` (and its `uuid4`/`Jsonb` needs) into
   `tests/db/external_boot_authority_support.py`; import it in
   `tests/db/test_external_boot_authority_migration.py`.
2. Write `tests/db/test_migration_0169_ended_allocation_authority_teardown.py`:

```text
"""Real-Postgres proofs that the authority teardown runs on an ended Allocation (#2992)."""

_FENCE = "v_allocation.state <> 'active'"
_RELAXED = "(v_allocation.state <> 'active' AND p_purpose <> 'teardown')"

def _end(conn, case, state): UPDATE allocations SET state = state WHERE id = case.allocation_id

def test_0169_patch_target_exists_once(pg_conn):
    _apply_through(pg_conn, "0168")
    for each of _ALLOCATE/_ACKNOWLEDGE/_COMMIT_SIGNATURE:
        definition.count(_FENCE) == 1 and _RELAXED not in definition

@pytest.mark.parametrize("allocation_state", ["released", "expired"])
def test_0169_teardown_completes_on_ended_allocation(...):
    case = _ready_teardown_case(migrated_url, suffix); _end(case, allocation_state)
    authority = _allocate(worker, case)                      # allocated
    _acknowledge(provider-authority role, case, authority) == "applied"   # real function
    then _make_current(...) for the head; finalize twice == "applied"
    system 'torn_down', one release row, no reservation row

@pytest.mark.parametrize("allocation_state", ["released", "expired"])
def test_0169_teardown_failure_commits_on_ended_allocation(...):
    seed as test_0160 `_admitted` (teardown, activation 'prepared', ready reservation);
    end the Allocation after allocate+acknowledge; failure commit == ("applied", "queued")

@pytest.mark.parametrize("purpose", ["activate", "recover", "resolve-conflict", "release"])
def test_0169_other_purposes_keep_the_allocation_fence(...):
    seed + _prepare_purpose_state on an active Allocation; allocate; acknowledge; then expire.
    a second allocate (attempt unchanged) == "superseded" and writes no authority row;
    acknowledging a fresh allocation made before expiry == "superseded", no ack row;
    commit of a `fail` result == ("authority_superseded", "failed"), activation and System
    state unchanged.
```

   The acknowledgement helper calls `acknowledge_external_boot_authority` with the 19 arguments
   in `_ACKNOWLEDGE_SIGNATURE` order, as `tests/db/test_external_boot_authority_migration.py`
   `_acknowledge` does (journal sequence 1, `_JOURNAL`, `_QUIESCENCE`).
3. Run the focused command; expect the ended-Allocation cases red (`superseded`).
4. Write the migration:

```sql
-- 0169_ended_allocation_authority_teardown.sql — #2992, ADR-0584/ADR-0620 amendments (2026-10-01).
-- Forward-only (ADR-0015). Admit purpose `teardown` on an Allocation in any state in the
-- allocator, the acknowledgement and the result commit (0122). Every other purpose keeps the
-- `active` fence. Each definition carries the fence literal exactly once.
DO $$
DECLARE
    v_old constant text := $old$v_allocation.state <> 'active'$old$;
    v_new constant text := $new$(v_allocation.state <> 'active' AND p_purpose <> 'teardown')$new$;
    v_function regprocedure;
    v_definition text;
BEGIN
    FOREACH v_function IN ARRAY ARRAY[<the three signatures>]::regprocedure[] LOOP
        v_definition := pg_get_functiondef(v_function);
        IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
           OR position(v_new IN v_definition) <> 0 THEN
            RAISE EXCEPTION 'external boot authority Allocation fence changed in %', v_function;
        END IF;
        EXECUTE replace(v_definition, v_old, v_new);
    END LOOP;
END
$$;
```

5. Update the four window-list tests and remove the 0168 expired test.
6. Run `just test-verbose` on the 0169, 0168, 0160, authority-migration, and the four list test
   files; expect all green. Commit.
7. Controlled fault: drop `AND p_purpose <> 'teardown'` from `v_new`, run the 0169 file, observe
   the other-purposes case red; `git checkout -- src/kdive/db/schema/0169_*.sql`.

## Task 2: the public route on an expired Allocation

Files: modify `tests/integration/test_external_boot_unrouted_teardown.py`.

Verification:
- Contract: `systems.teardown` on a System whose Allocation is `expired` queues the authority
  teardown and one worker pass leaves System and activation `torn_down`. Mode: focused-test.
  Add `"expired"` to the `residue` parametrization of `test_routed_teardown_completes_and_releases`:
  after the activate job is canceled, `UPDATE allocations SET state = 'expired'`; expect the
  same outcome, and the final `release_allocation` call to not report `released` (skip that
  assertion for this arm, asserting the Allocation stays `expired`). Red with the 0169 file
  removed: the job does not succeed. Green:
  `just test-verbose tests/integration/test_external_boot_unrouted_teardown.py`.

Steps: add the arm, run red (stash the migration), restore, run green, commit.

## Task 3: decision record and runbook

Files: `docs/adr/0584-provider-host-authority-fences-external-boot-mutations.md`,
`docs/adr/0620-authority-owned-system-teardown.md`,
`docs/operating/runbooks/stuck-tearing-down-system.md`.

Verification:
- Contract: amendments and runbook text. Mode: task-test-not-applicable — prose with no
  executable consumer; `just docs-links`, `just docs-paths`, and `just records` cover links,
  paths, and record structure.

Steps:
1. ADR-0584: `### Amendment (2026-10-01): a teardown generation outlives its Allocation (#2992)`
   — purpose `teardown` is allocated, acknowledged, and committed on an Allocation in any state;
   every other purpose keeps the `active` requirement; migration 0169; rejected alternatives
   (do nothing; admit only `released`/`expired`; relax only the allocator).
2. ADR-0620: a short amendment pointing to the ADR-0584 amendment and superseding the #3026
   amendment's "still answers `superseded`" sentence for this case.
3. Runbook: drop "runs only on an `active` Allocation", the `allocations.renew` step, and the
   expired/released limit; make the release step conditional on the Allocation still being
   `active`.
4. Run `just docs-links`, `just docs-paths`, `just records`; commit.
