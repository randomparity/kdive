# Bound the authority System retained-quarantine requeue (#3530)

## Scope and authority

Campaign scope for issue #3530, token `q3530-ecde9538`; the operator approved on 2026-11-04 the
exclusions journal headroom (#3526, merged) and unconfirmed-record recovery (#3531). Migration
number 0188 was assigned by the campaign.

## Problem

`finalize_authority_system_attempt` and `repair_terminal_authority_system_attempts`
(`0149_authority_owned_system_provisioning.sql`; no later migration redefines them) requeue a job
whose attempt ended in a `retained-quarantine` receipt and raise `max_attempts` by one when the
job is on its last attempt. The only stop is `max_attempts = 2147483647`. A provision that fails
on every attempt therefore retried about 129 times at roughly 3 s intervals (live observation in
#3524) before the journal ceiling of #3526 refused it, and a persistently failing teardown can
spend the journal's teardown reserve.

## Design

Migration `0188_bound_authority_system_retained_requeue.sql` adds
`authority_system_retained_budget_spent(jobs)`, true when the job has recorded 4 retained
receipts since `jobs.created_at` (`enqueue` resets `created_at` on a recycle, as 0165 relies on,
so a recycled budget counts afresh). Both functions are patched in place with the repository's
`pg_get_functiondef` + `replace` idiom and an exactly-one-anchor guard. When the budget is spent
the same UPDATE ends the job `failed` with `error_category='conflict'` and the lease columns
cleared (the 0164 shape for the external-boot teardown) instead of `queued`, and `max_attempts`
is not raised. The attempt is still superseded and consumed, and a provision still moves the
ownership to `repair-required`; the finalizer returns `('retained','failed',...)`.

### Choice of the cap: 4

The receipt is retained when the provider's observation is incomplete. Retries have no backoff,
so each costs about 3 s; a transient cause that clears in that window is covered by the first
retries. The journal reserves `TEARDOWN_RESERVE_RECORDS` = four attempts of eight records for a
teardown (#3526), so a cap of 4 also keeps a teardown that retains forever within that reserve.
One constant serves provision and teardown; a test pins it to the journal reserve. The count lives in the helper function, so changing
it is a new migration.

### Interaction with existing paths

- Provision: the job ends `failed`; ownership is `repair-required`. `systems.teardown` enqueues
  the preactivation teardown, and `request_authority_system_preactivation_teardown` accepts a
  `repair-required` owner whatever the provision job's state, so the System retires.
- Migration 0171 discharges an exhausted `running` provision job; a `failed` job needs no
  discharge. The `FAILED_OR_LAPSED_EXHAUSTED` recycle resets a failed external-boot teardown;
  the activation-free authority teardown replays its prior row (see Accepted limits).
- Late provision receipts on a `teardown-requested` owner still cancel (unchanged).

### Failure model

- Actors: the worker finalizer and the reconciler repair, both holding the per-System advisory
  lock and the job row lock.
- Invariants: at most 4 retained attempts (3 requeues, the 4th receipt fails the job) per job budget,
  and the bound is per budget, not per System: a recycled budget counts afresh; a failed job holds no lease or worker;
  the receipt is consumed once.
- Accepted limits: a teardown job that reaches the cap stays `failed` with the owner
  `teardown-requested`, and `enqueue_preactivation_teardown` (Python, outside this change's
  surface) replays a failed prior job instead of recycling it, so `systems.teardown` cannot retry
  it. Before this change the same System was stranded when the journal reserve ran out. The
  recycle belongs to a follow-up that changes `enqueue_preactivation_teardown` and the
  `request_authority_system_preactivation_teardown` queued-job check together.

## Validation

Real Postgres (`tests/db/test_migration_0188_bound_authority_retained_requeue.py`, driving the
migrated schema through the worker, server and reconciler roles):

- persistent provision failure stops at the cap, then teardown is admitted;
- persistent teardown failure stops at the cap;
- retained receipts below the cap still requeue; a recycled budget restarts the count;
- the reconciler repair ends an exhausted budget `failed`.

Controlled fault: removing the budget test from either function turns these red.
