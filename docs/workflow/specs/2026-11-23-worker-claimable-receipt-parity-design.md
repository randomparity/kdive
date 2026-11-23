# Match claimable depth to the authority receipt handoff

## Problem and boundary

The fully migrated depth function counts a job that claiming excludes because authority repair
owns its terminal unconsumed receipt. The real worker-role PostgreSQL reproduction returns
claim=[] and depth1. The existing140selected queue/retry/authority cases pass.
[ADR-0846](../../adr/0846-count-unconsumed-authority-receipts-consistently.md) records the scope.

## Change

Forward migration0206 amends only `count_claimable_worker_jobs(text[])`. Capture its installed
`pg_get_functiondef`; require exactly one accepted-lane predicate and no existing receipt fence.
Append the same job-correlated terminal/unconsumed `authority_system_attempts` exclusion used by
claiming, then execute the replaced definition. Reject unexpected or repeated source application.
Preserve the function identity, signature, owner, ACL, SECURITY DEFINER/search_path, validation,
clock, capture cleanup/publication/disposal and exhausted acknowledged-retry policy.
Leave `claim_worker_job` and all197applied migration files unchanged. No helper or new grant.

## Failure model

- A malformed source shape fails migration before changing a function; repair the migration
  precondition deliberately rather than silently applying the exclusion twice.
- Depth is observational. Receipt or job changes between counting and claiming may change the
  result; this does not reserve a claim or add locks to counting.
- A consumed receipt or unrelated job's receipt cannot suppress an otherwise eligible job.
- Existing clock choices and the separate query bodies remain; no generalized SQL policy or
  performance improvement is promised by this bounded receipt repair.
- Local PostgreSQL proof does not establish a real POWER deployment arm.

## Verification

Promote the original failure to real-role cases for queued and expired-running jobs with pending,
consumed or absent receipts. Preserve job/receipt rows across counting, compare count and claim,
and cover another job's pending receipt alongside an eligible control.
Upgrade from0205 and compare the full claim definition plus depth metadata excluding its body.
Require reapplication to fail without changing the resulting functions.
Existing exhausted-proof, worker-authority and capture tests protect untouched eligibility.
Fault the exclusion, consumption predicate and correlation separately; require named failures and
restore exact bytes. Run relevant gates/hooks, an actual built-wheel migration proof and fresh
scoped code/security reviews before resolving only the verified receipt-parity work.
