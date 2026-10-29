# 0714 — A repeated provider-conflict ends the external-boot job

## Status

Accepted (2026-10-01)

Implements [ADR-0711](0711-bound-acknowledged-retry-grant-per-budget.md) Decision 4 for #2901. It
does not change ADR-0711's limit, ADR-0584's fences, or ADR-0620's reservation credit.

## Context

The authority service reports every non-kdive exception at the provider boundary as
`provider-conflict` (`AuthorityService._provider_error`), and the provider's own text never
crosses the boundary (ADR-0584). The worker maps that reason to a retryable
`infrastructure_failure`, and each retry allocates a new authority generation. When the fault is
deterministic, such as the payload-cleanup defect in #2898, one job burns its whole budget in
generations. In the #2889 settle that was five generations in about 30 s.

ADR-0711 Decision 4 lets #2901 spend fewer attempts, for example by making a repeated identical
`provider-conflict` terminal. It must not raise `max_attempts` or add a grant. Today a requeue
erases the evidence a repeat check would need: the commit function clears the job's
`error_category` and `failure_context` (migration 0163, ADR-0019 envelope), and the audit row
records only `result_requeued`.

## Decision

1. The bound failure context gains `authority_reason: "provider-conflict"` when the authority sent
   that reason. The worker sets it from the sender error's details. The wire protocol is
   unchanged.
2. `commit_external_boot_authority_result` stores a `fail` result's `failure_context` on its
   audit row, in a new nullable column, only when the context carries `authority_reason`. The
   immutable audit row keeps nothing more than the repeat check reads.
3. A `fail` whose context carries `authority_reason` is terminal when the job's previous attempt in
   the current budget (`job_attempt = attempt - 1`, audit `created_at >=` job `created_at`)
   was requeued with an equal `failure_context`. The commit function makes this decision under
   the job-row lock that it already holds for the requeue-or-fail choice.
4. The threshold is two consecutive identical failures. One `provider-conflict` still retries.

## Consequences

- A deterministic provider fault that surfaces through the bound failure (the provider-call and
  commit phases) costs at most two generations per budget instead of `max_attempts`. The job ends `failed` with `infrastructure_failure`, and its failure context
  (`phase`, `authority_reason`) shows in `jobs.get`. A teardown job reaches the same end state as
  exhaustion, and the operator recycles it the same way.
- Two consecutive transient provider faults at the same phase end the job one or more attempts
  early. The authority cannot tell them apart from a deterministic fault. The cost is one manual
  recycle.
- Other failures still retry until `max_attempts`: transport failures, other peer reasons, other
  categories, and a repeat at a different phase.
- Audit rows from before the migration have no context. The first repeat after deploy therefore
  needs one more attempt.
- A provider-conflict raised during preparation, or during takeover acknowledgement, is raised
  before any bound failure exists and writes no `fail`. Its job still churns at lease-lapse pace,
  up to `max_attempts` plus the ADR-0711 grant (ADR-0593 wedge path). Covering it would need the
  activate `fail` precondition to admit a `preparing` activation, which is a fence change.
- Generic job retry (`fail_worker_job`) is unchanged. A generic job failure caused by this sender
  error now also shows `failure_detail_authority_reason` in its failure context.
- A worker running this code before the migration has its marked `fail` refused (`22023`). The
  job then stays `running` until reclaim, which is no worse than the churn before this change.

### Amendment (2026-10-21): a refusal the authority proves is a conflict skips the repeat check (#3327)

This qualifies the first consequence, which counts every provider-call refusal toward the two
identical failures. Under [ADR-0805](0805-authority-observed-conflict-is-recorded-from-its-journal-head.md)
the authority re-observes a refused `activate`, `recover` or `resolve-conflict` call. When that
observation is a `conflict`, it answers with the observation instead of `provider-conflict`, and
the worker records `recovery_conflict` after that one generation. Refusals over any other observed
state still reach this ADR's repeat check unchanged.

### Amendment (2026-10-27): a failed remote module PREPARE no longer leaves an unresolved operation (#3348)

This qualifies the consequence that a provider-conflict raised during preparation churns the job.
For a remote module PREPARE the cause was the PREPARE journal operation left at `mutation-started`
after the appliance failed; the next generation's `begin` and the System teardown were refused
because of it. The authority now closes that operation when the host reports the failure as
abandoned, after restoring the System's power ([ADR-0585](0585-remote-offline-module-restoration-appliance.md)
amendment of the same date). The retry runs instead of reaching the repeat check, and a teardown
is no longer refused for it. A failure the host could not abandon still leaves the operation
unresolved and churns as described above.

## Considered & rejected

- **Do nothing; rely on #2898's provider fixes.** judgment: the next deterministic provider defect
  would churn the same way, and ADR-0711 Decision 4 assigns this bound to #2901.
- **Make every `provider-conflict` terminal.** judgment: it drops the retry for a single transient
  provider fault, and the issue requires transient faults to stay retryable.
- **Bounded backoff between attempts.** verified: `jobs` has no not-before column
  (`rg -n "not_before|run_after|available_at" src/kdive/db/schema/` on main `ccc8b4329` finds
  none). Adding one changes the claim path for every job kind, which the exclusions leave to
  the operator, and backoff still spends every generation, only more slowly.
- **Keep the failure on the requeued job row.** verified: migration 0163 removed exactly that
  for the retained-teardown requeue, because the envelope forbids a category on a non-failure
  status.
- **Decide in the worker by reading the previous attempt.** verified:
  `SELECT has_table_privilege('kdive_worker','public.external_boot_authority_audit','SELECT')`
  returns `f` on a database migrated through 0168 (`postgres:17`, main `ccc8b4329`). A read
  outside the commit's job-row lock could also race a concurrent commit.
- **General no-progress detection across attempts.** judgment: the commit function has no
  per-attempt progress signal. Building one means reading journal heads or admitted mutations
  under the job-row lock, which is a larger change than comparing two bounded failure contexts.
- **Remember failures in the authority service process.** judgment: the memory would be lost on a
  restart and not shared across authority instances. Sending its verdict would also add a new wire
  reason.

### Amendment (2026-10-28): a release failure while recovering commits, and the retry adopts (#3396)

This closes a gap in the bound-failure path for a release, not in the repeat rule itself.

[ADR-0614](0614-derived-external-boot-release-phases.md)'s derived RECOVER moves a release's
activation to `recovering` before its restore. The release `fail` admission named no such state, so
every failure after that point was refused: the job kept `running` until its lease lapsed, on every
attempt, and the repeat check never ran.

Migration 0181 admits a release `fail` while the activation is `recovering` when the current
recovery attempt belongs to the failing authority's generation. The commit behaves as it does for
every other failure: the job is requeued, or fails when terminal or out of attempts, and the
authority is retired. A retired authority proves its job's attempt ended, so allocation and the
derived-release begin now adopt a retired prior authority of the same job and release, joined to
the current recovering attempt by generation, as they already adopted a `current` or `superseded`
one. Retiring rather than superseding keeps the commit's one behaviour for failures; a superseded
authority has a successor, and a failed attempt has none yet.

A failure in the window before a retry's own begin, after it has taken over but while the current
attempt is still its predecessor's, is still refused, and the job is recovered by the lease lapse as
before.

A terminal release failure that finds its recovery in flight (the final attempt, or a terminal
category) also closes the recovery, as the recover readiness branch does: the current attempt
becomes `failed` and the activation `recovery_failed`, both with terminal evidence, and the System's
open module obligations are discharged as a `terminal_escape`. `recovery_failed` claims only that
this release's recovery did not complete and will not be resumed. It does not claim the host is at
the source or the target state, because the restore may have stopped part way, and the evidence's
composite is the authority's quiescence digest from its takeover acknowledgement, not an observation
made after the failure. The discharge also lets the module-volume sweep reclaim the attempt's
storage before the System is torn down, as the recover readiness branch does. The release job is not
retried from `recovery_failed`; the reconciler's release lane still selects a `recovery_failed`
activation that holds a ready reservation, as it already did for one a recover produced, and its
handler refuses that state. The System is torn down from it, which is always admitted. A
non-terminal failure still requeues the job and leaves the activation `recovering` for the next
attempt to adopt.

### Amendment (2026-10-28): only `provider-conflict` carries `authority_reason` (#3391)

[ADR-0808](0808-a-definite-authority-rejection-ends-the-lifecycle-wait.md) adds `details["admission"]`
to other authority rejections. It does not add `authority_reason` to them, and it leaves
`failure_context` and the repeat check unchanged. A `provider-conflict` is still ambiguous to the
lifecycle wait, which ends at its deadline.
