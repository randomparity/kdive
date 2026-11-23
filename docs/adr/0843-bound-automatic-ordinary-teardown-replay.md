# 0843 — Bound automatic ordinary teardown replay to its current request

## Status

Proposed

## Context

The stalled-System repair recycles every ordinary terminal teardown with `TERMINAL`.
A real provider-failure proof through the teardown handler, worker and PostgreSQL exhausts
one job, then observes three successive repairs admitting it again: `[1, 1, 1]`.
Recycling resets its attempt counter. The per-incarnation worker limit never bounds this loop.
[ADR-0435](0435-reclaim-failed-provision-artifacts.md) preserves explicit operator retries;
[ADR-0483](0483-non-retryable-category-dead-letters-a-job.md) makes terminal escalation stronger
than category retryability. Automatic repair must preserve both contracts.

## Decision

An ordinary teardown payload may carry `automatic_teardown_replay_v1: consumed`.
Absence means its current request has not used automatic recovery. The existing job row and
payload replacement already delimit requests: an actual explicit failed-job recycle creates a
fresh payload; a live or idempotency-key replay returns the existing job without replacement.
No new table or migration is needed. First-enqueuer authorization remains unchanged.

For an ordinary teardown failure, the worker adds `teardown_recovery_reason` to the existing
redacted failure context using its actual terminal decision and the claimed payload:
`teardown_terminal_failure`, `teardown_automatic_replay_exhausted`, or
`teardown_automatic_replay_available`. Other job kinds and authority finalizers are unchanged.
This field supplies positive evidence; the reconciler cannot reconstruct terminal escalation
from a retryable category, even when the failure occurs on the last allowed attempt.

Under the existing System lock, retain all state, cancellation, authority-marker and unsettled
activation guards. Admit a missing teardown once with the consumed marker. Recycle an existing
row only when it is a valid same-System ordinary teardown, failed, attempt-exhausted, unconsumed,
with a retryable category and the worker's explicit available evidence. Use `FAILED`, not
`TERMINAL`. Write the consumed payload in the same existing enqueue transaction. Unknown old
failures, succeeded jobs and invalid payloads require operator inspection instead of replay.

Warn through the existing per-runner/page history with a stable reason and `systems.teardown`
remedy. The worker's exhausted reason remains durable on the failed job. Explain in the wrapper
and runbook that after correcting the cause an admin starts recovery with an omitted or new key;
reusing the original idempotency key preserves its recorded response. Keep explicit recycle audit.

## Consequences

A current request gets at most one automatic admission, with the job's existing attempt budget.
Restart and concurrent repair cannot replenish it. A new explicit request can recover again;
this is deliberate operator intent, not an automatic liveness guarantee. Unknown legacy evidence
fails closed. No wall-clock recovery deadline or promise that provider cleanup succeeds is added.
Cancellation, worker death and lease exhaustion without positive failure evidence require the
existing operator path. Page eviction or restart can repeat warnings, as in ADR-0842.

## Considered & rejected

- **Keep terminal recycling.** verified: the actual worker/provider proof admits `[1, 1, 1]`
  after separate exhausted failures; `queue.enqueue_with_status` resets terminal attempts.
- **Infer recoverability from category and attempts.** verified: worker `_is_terminal` also
  honors `CategorizedError.terminal`; the same category/attempt values can describe a permanent
  failure. Explicit finalizer evidence is required.
- **Persist a new System counter or request table.** judgment: the existing queue replacement
  already distinguishes fresh explicit requests from replays and atomically stores payload.
  Another owner needs extra reset writers and a migration without strengthening this limit.
- **Only use process memory.** verified: current warning/progress state is runner-owned and
  resets on restart; it cannot own a durable request allowance.
