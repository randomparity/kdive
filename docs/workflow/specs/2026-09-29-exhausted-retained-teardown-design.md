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
receipt by setting the job `queued` and superseding the authority (migration 0163 changed the
requeue's `error_category` from `'conflict'` to `NULL`; the stranding is unchanged). It does not look at the attempt counter. On the final attempt
(`attempt >= max_attempts`) the row is then `queued` and exhausted:

- `claim_worker_job` (0112, widened by 0150/0151) claims a `queued` row only when
  `attempt < max_attempts`, or when an exhausted row carries an acknowledged retry proof. That
  proof requires the job's `worker_id` to equal the authority's incarnation, and every writer
  that sets a marked job `queued` clears `worker_id` (0147, 0122, 0149, `queue.enqueue`);
- the reconciler's dead-letter (0162) selects `running` `boot` rows only;
- the public `systems.teardown` recycle (`JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED`,
  `src/kdive/jobs/queue.py`) resets a `failed` row or a lapsed exhausted `running` row, and the
  handler (`src/kdive/mcp/tools/lifecycle/systems/admin.py`) replays any other prior row.

So nothing moves the row again. The System keeps its admitted state and the reservation is
never credited.

A `failed` job alone is not enough. The public teardown first resolves the dispatch route with
`resolve_external_boot_system_teardown_dispatch_binding` (0147), which needs an authority for
the activation in state `current` or `retired`. A retained receipt leaves its teardown authority
`superseded`, and the teardown allocation superseded any `current` row. When the activation had
no `retired` authority, the route is empty and the handler refuses before the recycle. The
`fail` path does not have this gap, because `commit_external_boot_authority_result` (0122)
retires the authority on `fail`.

## Design

Migration `0164_dead_letter_exhausted_retained_teardown.sql` does two things.

1. **Finalizer.** It patches the finalizer with the repository's `pg_get_functiondef` +
   `replace` idiom (0149, 0160, 0161), with a shape guard that requires exactly one anchor. The
   anchor is the retained branch's `RETURN 'retained';`. Before that return it inserts:

   ```sql
   IF v_job.attempt >= v_job.max_attempts THEN
       UPDATE public.jobs SET state = 'failed', error_category = 'conflict',
           heartbeat_at = NULL, failure_context = '{}'::jsonb WHERE id = p_job_id;
       UPDATE public.external_boot_authorities
       SET state = 'retired', retired_at = clock_timestamp(), superseded_at = NULL
       WHERE id = p_authority_id;
   END IF;
   ```

   `v_job` is the job row the finalizer locked; the fence already requires
   `v_job.attempt = p_attempt`. The existing requeue and supersede statements stay unchanged, so
   a non-final attempt still requeues and supersedes. On the final attempt the job ends `failed`
   and the authority ends `retired`, which is what the `fail` path does at exhaustion (0122).
   The retired authority keeps the dispatch route. It cannot commit again, because every commit
   fence requires `current`. The receipt row and the `'retained'` return value are unchanged.
   The block sets `error_category` itself, so it does not depend on the category the requeue
   statement writes.
2. **Repair.** A one-time statement moves every row that is already stranded to the same state:
   a `teardown` job that is `queued` with `attempt >= max_attempts` and an
   `external_boot_authority_v1` object in `payload` becomes `failed`/`conflict`. The root
   authority of its `retained_quarantine` receipt for that same attempt becomes `retired` when it
   is `superseded` and was acknowledged. By the claim gate above, no worker can claim such a
   row, so no live attempt can race the repair.

Recovery is the existing supported path. A public `systems.teardown` sees a `failed` prior job
with the identical marker and a route. It recycles the job to a fresh `queued` attempt (attempt 0, the same
`max_attempts`) under the System lock. ADR-0620's #2884 amendment already proves that a reset
attempt counter cannot revive a superseded authority, and that the reservation credits once.

The worker is unaffected. After a `'retained'` finalize, the handler returns a derived
completion, and `Worker` (`src/kdive/jobs/worker.py`) returns without any job write for a marked
job. The job state stays owned by the finalizer.

## Failure model

1. **Actors and deployments**
   - the job worker that runs an authority-marked System teardown and calls the finalizer;
   - an operator or agent that calls `systems.teardown` through MCP;
   - the migration runner (`kdive.db.migrate`) that applies 0164 once per database.
2. **Invariants and assets at stake**
   - the ready reservation credits exactly once (ADR-0620), and the authority fences (ADR-0584,
     ADR-0620) keep their current behavior;
   - a `retained_quarantine` receipt on a non-final attempt still requeues the job;
   - the dispatch route of the activation survives a final-attempt retained receipt;
   - after 0164, no authority-marked teardown job is left `queued` with
     `attempt >= max_attempts` by the finalizer or by rows that existed before 0164.
3. **Accepted failure classes**
   - the dead-lettered job itself is re-run only by a public `systems.teardown` recycle, the
     recovery ADR-0620 already defines for a failed authority teardown. A reconciler repair lane
     (`reconciler/repairs/external_boot.py`) may enqueue a separate teardown successor for the
     activation, as it already does after a `fail` at exhaustion; that successor serializes
     through the per-System lock and the authority fences.
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
- **Keep the authority `superseded` and widen the dispatch route to `superseded` rows.**
  judgment: a `superseded` row can be one that lost an allocation race, so the route would
  depend on rows the fences already treat as dead.
- **Recycle a `queued` exhausted row in the public teardown.** judgment: it widens a generic
  queue policy for a state that one writer produces, and the row stays stranded until someone
  calls teardown.
- **Reconciler dead-letter lane for queued teardown.** judgment: a periodic sweep and a new
  security-definer function for a state that the finalizer can prevent in its own transaction.

## Success

- A `retained_quarantine` finalize on the final attempt leaves the job `failed` with
  `error_category = 'conflict'`, leaves the authority `retired`, returns `'retained'`, and
  leaves `resolve_external_boot_system_teardown_dispatch_binding` with one route for the
  activation, also when no other authority of the activation is `retired`.
- A `retained_quarantine` finalize on a non-final attempt leaves the job `queued`, as before.
- A job that was stranded before 0164 is `failed` after 0164 is applied, and its retained
  root authority is `retired`.
- A public-teardown recycle of the dead-lettered job, followed by a successful attempt, credits
  the reservation once. A replayed teardown after success credits nothing more.

## Validation

- `focused-test`: `tests/db/test_exhausted_retained_teardown.py`, final-attempt retained finalize
  gives job `failed`/`conflict`, authority `retired`, and one dispatch route from
  `resolve_external_boot_system_teardown_dispatch_binding` for a case with no other `retired`
  authority; red on main (job `queued`, no route).
- `focused-test`: same file, non-final retained finalize leaves the job `queued` and the
  authority `superseded`; green on main as well (a regression guard).
- `focused-test`: same file, a stranded job and its superseded retained root authority, seeded
  after the file-by-file apply through 0163 (the `_apply_through` pattern in
  `tests/db/test_migration_0070_resolved_cpu.py`), are `failed` and `retired` after
  `migrate.apply_migrations`; a marked non-exhausted `queued` teardown and an unmarked exhausted
  one are unchanged. Red without 0164.
- `focused-test`: same file, recycle of the dead-lettered job through `queue.enqueue` with
  `FAILED_OR_LAPSED_EXHAUSTED`, then an applied finalize, gives exactly one release row; a replay
  gives no second row. The handler half of the public call (a route plus a `failed` prior with
  the identical marker recycles) is the existing
  `tests/mcp/lifecycle/test_systems_tools.py::test_teardown_recycles_failed_authority_job_with_identical_marker`;
  the new route assertion above closes the gap between them.
- `focused-test`: the migration registration lists in `tests/db/test_migrate.py` and the three
  migration-tail tests include `0164`.
