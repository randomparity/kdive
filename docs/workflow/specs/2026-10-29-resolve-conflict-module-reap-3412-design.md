# Remote resolve-conflict reaps the restore's module volumes (#3412)

Decision record: [ADR-0620](../../adr/0620-authority-owned-system-teardown.md), amended by this
change.

## Problem

A remote `resolve-conflict` is committed by the authority through `commit_cleanup`, which recovers
the System and then runs `cleanup`. `cleanup` refuses ("remote module volume remains before
authorized cleanup") while the attempt's source or scratch volume exists. The worker's module
lifecycle maps `RESOLVE_CONFLICT` to `restore`, so the resolve restores the volumes and nothing
reaps them: the release that recorded the conflict ended `conflict` before its own CLEANUP. The
resolve always fails at the authority and its job is left running.

## Decision

Reap on the worker side, before the authority mutation, as a second step of the resolve's module
lifecycle. The alternative, reaping in place inside `commit_cleanup`, is rejected: the authority host
cannot discharge the reap obligation (only the worker holds the database fence), and the discharge
rules are the ones release CLEANUP already uses.

For `RESOLVE_CONFLICT` the worker runs `restore` then `reap`, both with the resolve's authority
request. `RECOVER` stays restore-only; `CLEANUP` stays reap-only.

- Wire: `RemoteModuleLifecycleRequestV1` admits a `reap` under `RESOLVE_CONFLICT`. The validator
  only; the JSON schema is unchanged.
- Worker evidence (`execute_remote_module_lifecycle_on_authority_host`): a reap under CLEANUP or
  RESOLVE_CONFLICT compares the host's completion with the retained restored evidence, and a
  retry after the obligation already discharged is accepted when the host reports the volumes
  absent. TEARDOWN keeps the PREP-baseline comparison.
- Host (`_reap`): the running-System admission added for CLEANUP applies to RESOLVE_CONFLICT. The
  reap still deletes only attempt volumes proven attached to no domain; TEARDOWN still requires
  the System off.
- Discharge: unchanged. `commit_worker_remote_module_evidence('discharge-reap')` is not
  operation-specific and the resolve job carries the same `remote_module_attempt_v1` receipt.

## Re-entry (after #3410)

A re-entered resolve repeats `restore` then `reap`. The host answers a repeated restore from its
retained restored evidence, without touching volumes, so a reap that already ran is not undone. A
reap that completed on the host but did not discharge is idempotent (state `reaped`), and a reap
that discharged is re-reported through the CLEANUP retry rule.

## Failure model

- Reap refused or fails: the resolve job fails before the authority mutation; nothing was
  recovered, the conflict stands and the resolve can be retried.
- Authority mutation fails after the reap: the volumes are gone and the obligation is discharged;
  the retried job repeats both steps harmlessly and the mutation proceeds.
- The authority mutation is refused after the reap (for example the observed composite went
  stale): the volumes are gone and the obligation is discharged while the conflict stands. A later
  restore is served from the retained evidence, a CLEANUP takes the discharged-retry path and
  teardown is admitted.
- An authority older than the worker rejects the reap at its wire validator, before any mutation;
  the resolve fails retryably until the authority is upgraded, so deploy the authority first.
- No restored evidence on the host: the reap is refused before mutation ("requires restored
  evidence").

## Tests

- Handler: the resolve-conflict lifecycle runs restore then reap; RECOVER restore only; CLEANUP
  reap only.
- Worker evidence: a resolve-conflict reap discharges, open or already discharged.
- Wire and host: the validator admits the reap, and the host reaps while the System runs.
