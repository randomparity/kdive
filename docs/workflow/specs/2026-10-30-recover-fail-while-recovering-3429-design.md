# Recover and resolve-conflict failures commit while recovering (#3429)

Decision record: [ADR-0714](../../adr/0714-repeated-provider-conflict-ends-the-external-boot-job.md),
amended by this change in its own section. Migration: `0185_recover_fail_while_recovering.sql`.

## Problem

`commit_external_boot_authority_result` returns `superseded` for two failure commits that a live
recover or resolve-conflict job produces. The worker logs the refusal and drops the result
(`_log_superseded_commit`, `src/kdive/jobs/worker.py`), so the job stays `running` until its lease
lapses, on every attempt, and ends `lease_expired`.

1. **Resolve-conflict while `recovering`.** Its first pass commits `recovery-attempt`, which moves
   the activation to `recovering`. The resolve-conflict `fail` admission accepts only
   `recovery_conflict`. Every later failure is refused, and the activation stays `recovering`.
   This is the #3410 run.
2. **Recover readiness failure while `active`.** `recover_handler.before_port` raises a terminal
   `readiness_failure` when the request deadline has passed before any recovery attempt exists. The
   recover readiness close branch then finds no `recovering` attempt and returns `superseded`.

Allocation already admits `recover` on `active`/`recovering` and `resolve-conflict` on
`recovery_conflict`/`recovering`. The `fail` admission is narrower than the allocation admission
for resolve-conflict. The recover readiness branch, not the admission, refuses case 2.

## Decision

Migration 0185 rewrites the function in place, the way 0181 does, with three text replacements.
Each replacement checks that its old text occurs exactly once and raises an exception if not.

- **Admission.** The resolve-conflict `fail` arm accepts `recovery_conflict` or `recovering`, the
  same states allocation admits. The recover arm is unchanged.
- **Terminal close.** The release-only close from 0181 now covers `release`, `recover` and
  `resolve-conflict`. When the failure is terminal and the activation is `recovering`, it marks the
  current attempt `failed` and the activation `recovery_failed`, both with
  `external-boot-terminal-evidence-v1` evidence (outcome `recovery_failed`). It also discharges the
  System's open module obligations as `terminal_escape`. A failure is terminal when the result is
  marked terminal, the job is on its last attempt, or it repeats the previous attempt's
  `provider-conflict` failure (the ADR-0714 rule). That third case can now close a recovery too.
- **Obsolete path removed.** The recover-only readiness branch is deleted. The widened close
  handles recover terminal failures while `recovering`, readiness included. A recover failure on
  an `active` activation no longer has a branch that can refuse it. It commits, the job fails, and
  the activation stays `active`.

Unchanged: the release arm and its ownership check (#3396, excluded); the worker's SUPERSEDED
handling (excluded); a resolve-conflict failure on `recovery_conflict` (the activation stays
parked); a non-terminal failure while `recovering` (the job requeues and the next attempt takes
over the recovery, which allocation already admits).

Recover and resolve-conflict do not need release's same-job generation check. Allocation lets
either purpose take over any `recovering` attempt, and the handler re-runs it. The job that holds
the one `current` authority (`external_boot_authorities_one_current_per_system`) owns the
recovery when it fails.

## Failure model

1. **Actors and deployments:** the worker's `kdive_worker` role committing a failure for a
   local-libvirt or remote-libvirt external-boot job; the migration runner applying 0185.
2. **Invariants and assets at stake:** an activation is `recovering` exactly while its current
   attempt is; `recovery_failed` is terminal and is not resumed; module obligations are discharged
   only on a terminal escape; the earlier rewrites (0128 through 0182) survive the in-place
   rewrite.
3. **Accepted failure classes:**
   - A resolve-conflict that finds the activation `recovering` because of another job's abandoned
     recovery now closes that recovery `recovery_failed`. This is accepted because allocation
     already lets the job take that recovery over, and the job holds the only `current` authority.
   - `recovery_failed` evidence records the takeover quiescence digest, not an observation made
     after the failure. This is the existing 0181 and recover-branch semantics.
4. **Covered elsewhere:** release fail while recovering (0181, ADR-0714 #3396 amendment); the
   worker SUPERSEDED retry for other job kinds (excluded); leaving `recovery_failed`, which
   `systems.teardown` always admits.

## Success

- (C1) A resolve-conflict `fail` on a `recovering` activation commits (`applied`).
- (C2) A terminal recover or resolve-conflict `fail` on a `recovering` activation marks the attempt
  `failed` and the activation `recovery_failed` with outcome `recovery_failed`, and discharges
  obligations as `terminal_escape`. A failure that is non-terminal but uses the job's last attempt
  behaves the same way.
- (C3) A terminal recover `readiness_failure` on an `active` activation commits, the job fails, and
  the activation stays `active` with no attempt row.
- (C4) A non-terminal resolve-conflict `fail` on a `recovering` activation requeues the job and
  leaves the activation and attempt `recovering`.
- (C5) Each 0185 replacement target occurs exactly once in the function definition that the earlier
  migrations produce.

## Validation

Connected tests in `tests/db/test_migration_0185_recover_fail_while_recovering.py` are written
first. The plan's Task 1 Verification lists each test, the criterion it proves, and its red state
before 0185. `test_resolve_conflict_failure_applies_after_evidence_only_acknowledgement` and
`test_worker_authority_terminal_recovery_failure_discharges_mutations` already cover a failure on
`recovery_conflict` and a terminal recover readiness close while `recovering`; both must stay
green. The four migration window tests gain the 0185 row and K+1.
