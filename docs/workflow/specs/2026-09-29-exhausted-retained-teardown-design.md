# An exhausted retained System teardown is dead-lettered, not stranded `queued` (#2917)

## Scope and authority

Campaign scope for issue #2917, token `q2917-7593f7c2`. The operator approved these exclusions on
2026-09-29: the tool envelope `from_job` `error_category` defect (#2916); deterministic
provider-conflict retry churn (#2901); repair-lane teardown exhaustion and exhausted
`authority_system_v1` jobs (operator). The decision is recorded as a dated amendment to
[ADR-0620](../../adr/0620-authority-owned-system-teardown.md), beside its #2884 and #2889
amendments.

## Problem

`finalize_external_boot_authority_teardown` (migration 0147) handles a `retained_quarantine`
receipt by setting the job `queued` with `error_category = 'conflict'` and superseding the
authority. It does not look at the attempt counter. On the final attempt
(`attempt >= max_attempts`) the row is then `queued` and exhausted:

- `claim_worker_job` (0112) claims a `queued` row only when `attempt < max_attempts`;
- the reconciler's dead-letter (0162) selects `running` `boot` rows only;
- the public `systems.teardown` recycle (`JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED`,
  `src/kdive/jobs/queue.py`) resets a `failed` row or a lapsed exhausted `running` row, and the
  handler (`src/kdive/mcp/tools/lifecycle/systems/admin.py`) replays any other prior row.

So nothing moves the row again. The System stays `ready` and the reservation is never credited.

## Design

Migration `0163_dead_letter_exhausted_retained_teardown.sql` does two things.

1. **Finalizer.** It patches the finalizer with the repository's `pg_get_functiondef` +
   `replace` idiom (0149, 0160, 0161), with a shape guard that requires exactly one anchor. The
   anchor is the retained branch's `RETURN 'retained';`. Before that return it inserts:

   ```sql
   UPDATE public.jobs SET state = 'failed', error_category = 'conflict'
   WHERE id = p_job_id AND attempt >= max_attempts;
   ```

   The existing requeue statement stays unchanged, so a non-final attempt still requeues. The
   receipt row, the authority supersession, and the `'retained'` return value are unchanged.
   The new statement runs in the same transaction, under the job row lock the finalizer
   already holds. It sets `error_category` itself, so it does not depend on the category the
   requeue statement writes.
2. **Repair.** A one-time `UPDATE` moves every row that is already stranded to the same state:
   `kind = 'teardown'`, `state = 'queued'`, `attempt >= max_attempts`, and an
   `external_boot_authority_v1` object in `payload`. The worker cannot claim such a row, so no
   live attempt can race the update.

Recovery is the existing supported path. A public `systems.teardown` sees a `failed` prior job
with the identical marker. It recycles the job to a fresh `queued` attempt (attempt 0, the same
`max_attempts`) under the System lock. ADR-0620's #2884 amendment already proves that a reset
attempt counter cannot revive a superseded authority, and that the reservation credits once.

The worker is unaffected. After a `'retained'` finalize, the handler returns a derived
completion, and `Worker` (`src/kdive/jobs/worker.py`) returns without any job write for a marked
job. The job state stays owned by the finalizer.

## Failure model

1. **Actors and deployments**
   - the job worker that runs an authority-marked System teardown and calls the finalizer;
   - an operator or agent that calls `systems.teardown` through MCP;
   - the migration runner (`kdive.db.migrate`) that applies 0163 once per database.
2. **Invariants and assets at stake**
   - the ready reservation credits exactly once (ADR-0620), and the authority fences (ADR-0584,
     ADR-0620) keep their current behavior;
   - a `retained_quarantine` receipt on a non-final attempt still requeues the job;
   - after 0163, no authority-marked teardown job is left `queued` with
     `attempt >= max_attempts` by the finalizer or by rows that existed before 0163.
3. **Accepted failure classes**
   - a dead-lettered teardown stays `failed` until a public `systems.teardown` recycles it. No
     reconciler lane re-runs it. This is the recovery that ADR-0620 already defines for a failed
     authority teardown.
   - each recycle grants a full `max_attempts` budget again. Deterministic retained churn can
     therefore exhaust it again. The operator sees `failed` and can call teardown again. The
     churn itself is #2901.
4. **Covered elsewhere**
   - the envelope error for a `queued` row that carries a category: #2916;
   - retry churn that reaches exhaustion: #2901;
   - exhausted `authority_system_v1` jobs and repair-lane teardown exhaustion: operator;
   - the live settle on a native ppc64le (POWER9) KVM-HV host: the #2898 settle after merge.

## Considered and rejected

- **Grant one more attempt at exhaustion (as 0149 does for authority Systems).** judgment: under
  deterministic retained churn (#2901) the job retries without a bound, and the operator never
  sees a terminal state.
- **Recycle a `queued` exhausted row in the public teardown.** judgment: it widens a generic
  queue policy for a state that one writer produces, and the row stays stranded until someone
  calls teardown.
- **Reconciler dead-letter lane for queued teardown.** judgment: a periodic sweep and a new
  security-definer function for a state that the finalizer can prevent in its own transaction.

## Success

- A `retained_quarantine` finalize on the final attempt leaves the job `failed` with
  `error_category = 'conflict'`, and returns `'retained'`.
- A `retained_quarantine` finalize on a non-final attempt leaves the job `queued`, as before.
- A job that was stranded before 0163 is `failed` after 0163 is applied.
- A public-teardown recycle of the dead-lettered job, followed by a successful attempt, credits
  the reservation once. A replayed teardown after success credits nothing more.

## Validation

- `focused-test`: `tests/db/test_exhausted_retained_teardown.py`, final-attempt retained finalize
  gives `failed`/`conflict`; red on main (the row stays `queued`).
- `focused-test`: same file, non-final retained finalize stays `queued`; green on main as well
  (it guards against a regression).
- `focused-test`: same file, a row stranded before the 0163 migration is `failed` after
  `migrate.apply_migrations`. The row is seeded after the file-by-file apply through 0162
  (the `_apply_through` helper pattern in `tests/db/test_migration_0070_resolved_cpu.py`). Red
  without 0163.
- `focused-test`: same file, recycle of the dead-lettered job, then an applied finalize, gives
  exactly one release row, and a replay gives no second row.
- `focused-test`: the migration registration lists in `tests/db/test_migrate.py` and the three
  migration-tail tests include `0163`.
