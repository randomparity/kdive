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
2. `commit_external_boot_authority_result` stores the `fail` result's `failure_context` on its
   audit row, in a new nullable column.
3. A `fail` whose context carries `authority_reason` is terminal when the job's previous attempt in
   the current budget (`job_attempt = attempt - 1`, audit `created_at >=` job `created_at`)
   was requeued with an equal `failure_context`. The commit function makes this decision under
   the job-row lock that it already holds for the requeue-or-fail choice.
4. The threshold is two consecutive identical failures. One `provider-conflict` still retries.

## Consequences

- A deterministic provider fault costs at most two generations per budget instead of
  `max_attempts`. The job ends `failed` with `infrastructure_failure`, and its failure context
  (`phase`, `authority_reason`) shows in `jobs.get`. A teardown job reaches the same end state as
  exhaustion, and the operator recycles it the same way.
- Two consecutive transient provider faults at the same phase end the job one or more attempts
  early. The authority cannot tell them apart from a deterministic fault. The cost is one manual
  recycle.
- Other failures still retry until `max_attempts`: transport failures, other peer reasons, other
  categories, and a repeat at a different phase.
- Audit rows from before the migration have no context. The first repeat after deploy therefore
  needs one more attempt.
- Generic job retry (`fail_worker_job`) is unchanged.

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
- **Remember failures in the authority service process.** judgment: the memory would be lost on a
  restart and not shared across authority instances. Sending its verdict would also add a new wire
  reason.
