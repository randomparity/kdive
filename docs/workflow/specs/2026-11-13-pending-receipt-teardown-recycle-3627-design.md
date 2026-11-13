# A pending absence receipt keeps its preactivation teardown (#3627)

Decision record: [ADR-0620](../../adr/0620-authority-owned-system-teardown.md), amendment
"A pending absence receipt keeps its preactivation teardown (#3627)".

## Problem

`systems.teardown` on a never-activated authority-owned System passes
`FAILED_OR_LAPSED_EXHAUSTED` to `enqueue_preactivation_teardown`
(`src/kdive/services/systems/authority_owned.py`). The queue's recycle `UPDATE`
(`src/kdive/jobs/queue.py`) restarts a `running` row whose final attempt's lease lapsed, and a
`failed` row. It does not know whether that attempt already recorded the authority's
`preactivation-absent` receipt. When it did, and the worker died before finalizing, the
reconciler's teardown repair (`list_authority_system_teardown_repairs` /
`finalize_authority_system_teardown_repair`, migration 0149) owns the row: it consumes the
receipt and marks the job `succeeded` only while it is `running`. A restarted row is `queued`,
so it stays behind the claim fence, is later claimed and refused against the `torn-down` owner.
The server role cannot read `authority_system_attempts`, so the service cannot see the receipt.

## Design

1. **Migration `0196_authority_system_receipt_pending.sql`** adds
   `authority_system_preactivation_receipt_pending(p_system_id uuid) RETURNS boolean`,
   `plpgsql`, `SECURITY DEFINER`, `SET search_path = ''`, volatile. It:
   - raises `42501` unless `session_user` is a member of `kdive_server`;
   - takes `pg_advisory_xact_lock(hashtextextended('kdive:system:' || id, 2125))`, the lock
     every receipt writer (`advance_authority_system_journal_head`), the worker finalizer and
     the teardown repair take first;
   - returns whether the System's current attempt (`ownership.current_attempt_id`) is
     `state = 'terminal'`, `consumed_at IS NULL`, `receipt_disposition = 'preactivation-absent'`,
     with `ownership.state = 'teardown-requested'` and the ownership journal head equal to the
     attempt's terminal head. These are the receipt clauses of the repair candidate predicate,
     copied verbatim. The repair's job-liveness disjunct is omitted: the recycle only admits a
     `failed` row or one whose lease lapsed, and either satisfies that disjunct.
   - `REVOKE ALL` from `PUBLIC` and every kdive role, then `GRANT EXECUTE` to `kdive_server`
     only. Every caller passing a policy other than `NEVER` runs as the server
     (`systems.teardown`, `ops.force_teardown`); the reconciler and job-cancel callers pass
     `NEVER` and never call it.
2. **`enqueue_preactivation_teardown`**, after the marker check and before
   `queue.enqueue_with_status`: when a prior row exists and `recycle` is not `NEVER`, call the
   function; when it returns true, set `recycle = NEVER`. The queue then returns the row
   unchanged, `request_authority_system_preactivation_teardown` returns `replay`, `changed` is
   false, and `on_recycle` does not run. `queue.py` stays free of authority tables.
3. **Lock order.** The caller already holds the Python System lock; the function adds the SQL
   System lock before the job-row `UPDATE`. `request_authority_system_preactivation_teardown`
   takes the same SQL lock later in the same transaction, so the order Python lock → SQL lock
   → job row is unchanged, and the SQL lock now precedes the job row as it does in the repair.
   Held to commit, it keeps the answer true for the recycle statement: no receipt can be written
   or consumed between the read and the `UPDATE`.
4. **Records.** Append an ADR-0620 amendment qualifying the #3617 amendment's "One case is
   accepted" paragraph and its "No schema change" for that case. Reduce the runbook
   (`docs/operating/runbooks/exhausted-authority-system-provision.md`) step that tells the
   operator to watch the reconciler log before running `systems.teardown`.

Both `ops.force_teardown` (`FAILED`) and `systems.teardown` inherit the guard; neither caller's
policy changes. A `failed` row with a pending receipt is also left for the repair.

## Success

- S1: an exhausted, lapsed `running` preactivation teardown whose current attempt holds an
  unconsumed `preactivation-absent` receipt at the ownership head is returned unchanged by
  `systems.teardown` (state, attempt, `max_attempts`), writes no `teardown:recycled` audit, and
  the reconciler repair then settles it `succeeded` with the System `torn_down`.
- S2: the same row with no such receipt still restarts (`queued`, `max_attempts` 3 → 6), as in
  #3617's existing test.
- S3: the function answers false for each single deviation from the predicate (consumed
  receipt, other disposition, ownership not `teardown-requested`, head mismatch, not the current
  attempt, unknown System).
- S4: only `kdive_server` may execute it; `kdive_worker`, `kdive_reconciler` and
  `kdive_provider_authority` get `InsufficientPrivilege`.
- S5: the function blocks while another transaction holds the System's SQL lock.

## Failure model

1. **Actors and deployments**
   - An operator with project ADMIN calling `systems.teardown`; an operator calling
     `ops.force_teardown`. Server role, single Postgres, any kdive deployment.
   - The reconciler teardown repair and the provider authority, concurrently.
2. **Invariants and assets at stake**
   - A pending receipt is consumed by exactly one path (worker finalize or reconciler repair).
   - The server gains no read of `authority_system_attempts` beyond this one boolean.
   - #3617's recycle of an exhausted, lapsed attempt with no pending receipt.
3. **Accepted failure classes**
   - A receipt written after this transaction commits, onto an attempt whose worker is still
     active but whose job was restarted: the provider authority records it without a lease
     check. Accepted: no receipt existed when the operator asked, so the restart is #3617's
     intended behaviour; the repair still consumes the late receipt and the restarted row is
     refused against the `torn-down` owner, the outcome #3617 already accepted.
   - The function blocks behind a long-held SQL System lock. Bounded: the same lock is already
     taken later in this transaction by `request_authority_system_preactivation_teardown`.
4. **Covered elsewhere**
   - `ops.force_teardown` lapsed recycle: rejected by ADR-0620; #3628 documents it.
   - The repair settling a `queued` row: out of scope per the frozen charter.
   - Mapping an attempt id to a System for log lines: out of scope per the frozen charter.

## Threat model

- **Boundary widened:** the server role gains EXECUTE on one `SECURITY DEFINER` function that
  reads `authority_system_attempts`, a table revoked from it. No boundary is added.
- **Actors:** an authenticated operator reaching it only through `systems.teardown` or
  `ops.force_teardown` (ADMIN or breakglass checks run first); a compromised server process
  holding the server DSN.
- **Control:** the in-function `pg_has_role(session_user, 'kdive_server')` check plus the
  REVOKE/GRANT pair. The input is a `uuid`; the output is one boolean about one System's
  current attempt, no receipt bytes or digests. It writes nothing; its only side effect is the
  transaction-scoped System lock, which the server can already take through
  `request_authority_system_preactivation_teardown`.
- **Out of scope:** a server-DSN holder learning whether some System has a pending receipt.
  Accepted: the same holder can already read job state, ownership state and the consumed proof
  (`authority_system_preactivation_absence_proven`).

## Validation

- `tests/db/test_migration_0196_authority_system_receipt_pending.py` — S3, S4, S5, migration
  registered once.
- `tests/services/systems/test_authority_admission.py` — S1 (new case) and S2 (existing
  `test_exhausted_preactivation_teardown_recycles_only_after_lease_lapse[lapsed]`, unchanged).
- The four migration-window list tests (see the plan file map) gain the 0196 row.
