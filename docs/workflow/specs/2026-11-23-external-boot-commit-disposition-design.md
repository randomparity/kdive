# External-boot commit continuation disposition

## Problem

The worker re-infers whether a committed authority checkpoint should continue from operation
strings and a returned Job. The queue adapter has the typed result and database classification,
but its return value does not distinguish continuation from an ordinary returned row.
This is an internal ownership refactor; existing checkpoint continuation is already working.

## Scope

Implement [ADR-0853](../../adr/0853-external-boot-commit-continuation-disposition.md) in
jobs/queue.py and worker.py. Add one frozen process-local outcome with a Job and a boolean.
The queue adapter becomes the policy owner; the worker consumes the disposition. Migrate
its five direct test consumers and success mocks. Keep the failure adapter's Job-or-status
contract, status enums and the conflict adapter. Retain the public tool/provider/domain wire
schemas and the 198 applied SQL files. Add only an ADR-0845 supersession banner at acceptance.
Other campaign review owners remain independent.

### Failure model

- Prevent: continuing a classified refusal or terminal commit; dropping continuation of either
  authenticated running checkpoint; drifting worker and queue operation lists.
- Detect: exercise the actual queue decision with typed carriers and recorded database replies,
  independently of worker mocks; retain actual worker/Postgres claim and refusal regressions.
- Recover: preserve current missing-row, lost-fence and classified failure paths, cancellation
  propagation, lease recovery and the bound. No new retry or recovery action is introduced.
- Accept: database or serialization failures retain their existing exception behavior; this
  refactor adds no availability, timing, provider-completion or hardware guarantee.

## Success

1. A returned Job is carried by the internal outcome. Only an applied running Job with a
   typed success DeadlineResult or RecoveryAttemptResult permits continuation. A failed/queued
   classified reread always stops, even if its later row looks running. Missing rows and
   non-applied status enums preserve their current outcomes.
2. complete_external_boot exposes the outcome; fail_external_boot returns its carried Job or
   original status. Direct success consumers inspect outcome.job and continue_claim without
   losing their durable state/error checks. Conflict commits retain their existing contract.
3. The worker consumes continue_claim and removes its operation-name predicate. Marker and
   authority-binding checks still precede committing. The existing cap remains two handler
   invocations per claimed attempt; a second checkpoint logs and leaves its lease for recovery.
   No wall-clock limit or extra invocation is added.
4. SQL calls/parameters/transaction framing, role/fence checks, wire models, telemetry,
   cancellation, failure classification and lease/attempt identity are unchanged.

## Validation

Add a queue outcome matrix to tests/jobs/test_external_boot_authority_models.py, reusing its
recording DB boundary and real typed carriers. Cover both checkpoints, terminal and non-checkpoint
success, failed/queued classification, refusal statuses, missing status/rows and failure unwrapping.
New internal outcome assertions fail before implementation; this is a new adapter contract test,
not a claim that the prior worker behavior failed. Migrate success mocks to explicit outcomes.
Retain actual connected worker tests for both checkpoint types, terminalization, refusal and
at-most-two invocations, including matching job/attempt/lease identity and provider effects.
Use controlled disposition faults to show the new checks bite, then restore exact source.
Run relevant jobs/handler/connected-service/DB regressions with Docker required, whole lint/type,
hooks and documentation/record guards. Verify source/immutable SQL against the built installed
wheel and exercise the new queue disposition there. Fresh scoped code/security reviews precede
health closure. No operator stack, provider or foreign hardware execution is claimed.
