# Bounded System repair progress

## Problem

The leaked-mutation and stalled-reprovision lanes retry an ordered first page indefinitely
when its candidates keep failing. Real Postgres lock-timeout reproductions show both
miss a healthy 101st row across two `Reconciler.run_once` calls.

## Scope

Implement [ADR-0852](../../adr/0852-bounded-system-repair-lane-progress.md) in the two
existing repair loops and their existing runner progress carrier. Retain the teardown
hint and its public import/constructor/standalone keyword contracts. Add separate nullable
UUID hints for the two affected lanes, shared only within a runner. Existing direct
repair calls remain valid; repeated standalone callers may explicitly supply progress.
No cursor table, provider work, eligibility change, role/grant change or migration is needed.
Other quality findings remain in the campaign plan.

### Failure model

- Prevent: persistent first-page failures starving later candidates in a stable finite
  eligible set; another lane or runner advancing its cursor; loss of interrupted work.
- Detect: real Postgres lock contention, selection failures and cancellation; retain
  existing isolated warning/error reporting and transaction rollback.
- Recover: advance after handled failures, wrap only on an empty tail, retry earlier IDs
  after wrap, and retain the previous hint after failed selection or interrupted candidate.
- Accept: no durable progress across restart or wall-clock bound; newly eligible lower IDs
  wait for wrap; overlapping manual passes may revisit rows under existing database guards.

## Success

1. Each affected lane inspects at most 100 candidates per pass with its own NULL-aware
   UUID keyset position. At most two selects produce one nonempty page. A partial tail
   is not filled from the beginning; an all-empty lookup clears only that lane's hint.
2. A healthy candidate after 100 persistently locked candidates is repaired on the second
   runner pass. Guarded skips and handled exceptions advance; cancellation leaves the
   interrupted candidate retryable. Query failure preserves state.
3. Runner/lane hints remain independent, including runners sharing pool/config. The
   existing teardown hint, warning ownership and standalone behavior remain compatible.
4. Candidate transactions retain their locked eligibility, activation/job exclusions,
   mutation-discharge function, failure category, device updates, audit and count semantics.

## Validation

Use focused real-Postgres tests for both lanes: the 101-row runner reproduction, independent
runners/lanes, wrap/retry, partial tail, UUID zero, empty population, failed selection,
lock cancellation and guarded rechecks. Prove cancellation through a real ungranted advisory
lock and await cleanup. Existing role, exclusion, rollback and teardown-progress suites
protect safety behavior. Re-run the isolated private original reproduction after the fix.
Controlled faults to cursor advance, query predicate or lane wiring must fail the relevant
new tests; restore source before final gates. Run relevant reconciler tests with Docker
required, whole-tree lint/type/hooks, inspect actual wheel source and unchanged applied SQL.
Fresh independent scoped code/security reviews must approve before health closure.
No live provider or host stack is exercised or claimed by these database-only repairs.
