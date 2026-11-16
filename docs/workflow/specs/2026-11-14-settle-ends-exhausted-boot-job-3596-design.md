# Settlement ends the stranded boot job

Issue #3596. Decision record: the 2026-11-14 (#3596) amendment to
[ADR-0816](../../adr/0816-stranded-activation-settlement.md).

## Problem

A `runs.boot` that strands a fixed-worker System leaves its authority-marked `boot` job `running`
after its last attempt. `dead_letter_unowned_external_boot_jobs` (migration 0165) skips it while
its authority row is `allocating` or `current` and no acknowledged-retry grant was spent, and
`repair_abandoned_jobs` excludes authority-marked jobs. Nothing ends it, so
`settle_stranded_external_boot_activation` (migration 0194) refuses `job_live`, and the runbook
asks the operator to run `jobs.cancel` by hand, then observe and settle again.

## Owner

The settlement ends the job, not the reconciler. `settle` already runs as `platform_admin` with a
recorded reason, under the System lock, and it already supersedes the activation's `allocating`
and `current` authority rows in the same transaction. The reconciler has no operator authority,
and teaching its sweep the stranded shape means amending the 0165 `CONTINUE` bound, an approved
non-goal.

## Design

Migration 0198 re-creates `settle_stranded_external_boot_activation` from its stored definition by
three exact, asserted text replacements, as migration 0150 did for the claim functions. Signature,
owner, grants and every other refusal are unchanged.

1. **Exhausted set.** Where the function returns `job_live` today, it first locks, with
   `FOR UPDATE NOWAIT`, the jobs that satisfy all of:
   - `kind = 'boot'`, `state = 'running'`, `attempt >= max_attempts`,
     `lease_expires_at < clock_timestamp()`;
   - the marker's `system_id` is the System and its `activation_id` is the settled activation;
   - `NOT has_acknowledged_external_boot_retry_proof(job)`: no worker can claim it again through
     an unconsumed grant (migration 0150's claim gate).

   A lock it cannot take returns `job_live`. Any other `queued` or `running` authority-marked job
   for the System still returns `job_live`.
2. **End.** In the write phase, beside the existing authority-row supersession and only after
   every refusal has passed, each locked job becomes `failed` with `error_category =
   'lease_expired'`, and its marker's Run becomes `failed` with `failure_category =
   'lease_expired'` when it is `created` or `running`. These are the values the 0165 dead-letter
   path writes.
3. **Audit.** Each ended job adds one `platform_audit_log` row: tool
   `external_boot.settle_stranded_activation`, scope `failed_job:<job-id>`, the settlement's
   principal, session and actor, and an args digest over the activation id, job id and reason.

A refusal returns before step 2, so it ends no job; its locks end with the caller's transaction.
No Python code changes: the status set is unchanged, and the tool already returns `settled`.

The runbook's Recover step 2 and `job_live` row drop the manual cancel. Its
`authority_objects_present` row drops "yet (#3587)", keeping the manual instruction.

## Failure model

1. **Actors and deployments**
   - A `platform_admin` operator running `ops.recover_stranded_activation` `settle` (server role).
   - The reconciler's dead-letter sweep, workers claiming, heartbeating or completing jobs, and the
     authority advancing its journal, concurrently, against the same Postgres.
2. **Invariants and assets at stake**
   - A job a worker holds (live lease) or can still claim (attempts remaining, or an unconsumed
     acknowledged-retry grant) is never ended.
   - A refused settlement changes no job, Run or activation.
   - Every ended job is audited with the settlement's principal, actor and reason.
3. **Accepted failure classes**
   - A worker or the sweep holding the job row lock makes settle refuse `job_live`: transient;
     the operator runs `observe` and `settle` again (NOWAIT, as the 0165 sweep does for
     authority rows).
   - Settle locks the job row before the journal-head and authority rows, the reverse of the
     authority commit paths. No path is known that holds an authority row of a dead, lapsed job
     and waits on its row; if one deadlocks, Postgres aborts one transaction whole and nothing
     changes, so the operator reruns.
   - A heartbeat that renews a lapsed lease before settle locks the row keeps the job live: the
     worker evidently holds it.
   - An exhausted job of an earlier activation of the same System still refuses `job_live`; the
     stranded shape's job belongs to the activation being settled.
4. **Covered elsewhere**
   - Other authority-marked kinds, and exhausted jobs outside the stranded shape: the 0165 sweep
     (ADR-0711); changing its bound is an approved non-goal.
   - Objects still held on the authority: manual, #3587 closed not planned.

## Validation

Real-database tests in the plan's Task 1 (migration contract, registry) and Task 2 (the
operator's tools settle with no `jobs.cancel`); the live proof once the campaign grants its slot.
