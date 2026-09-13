# Handler worker-role tests design

## Scope

Issue #2347 converts job-handler test execution from the migration-owner connection to the shared
`kdive_worker` LOGIN pool. Owner access remains limited to seeding and observing database state.
The work changes tests and test documentation only; it does not alter runtime grants, production
handlers, schema, or platform error translation.

## Decision

[ADR-0651](../../adr/0651-handler-tests-use-worker-role-pools.md) owns the boundary: a converted
test passes the shared `kdive_worker_pool` to the handler act phase, while its fixture setup and
postcondition reads remain on owner connections. Helpers that previously accepted one owner pool
for both purposes gain explicit owner/worker inputs when necessary.

The conversion covers each `tests/jobs/handlers/` file that constructs an owner-backed pool for a
handler invocation. Files that only use `migrated_url` to seed, inspect, or test non-handler
helpers remain owner-only and are named in the implementation inventory.

## Acceptance criteria

1. Converted handler calls use the real worker LOGIN pool; setup and assertion reads still use the
   owner connection where the worker lacks the required privilege.
2. A controlled grant-less handler write raises raw `psycopg.errors.InsufficientPrivilege` through
   a converted handler test, and the controlled fault is removed before delivery.
3. The handler test tree passes in its normal xdist topology. The PR records before/after timings
   and does not introduce per-test role creation.
4. Any discovered production grant leak is reported as evidence and is not repaired in this PR.

## Failure handling

An `InsufficientPrivilege` raised by a converted test is evidence of a missing production grant.
The test remains red until the responsible issue is recorded; this issue does not widen grants or
map the exception. The shared fixture closes the worker pool and drops its LOGIN principals after
each fixture lifetime.

## Verification

Run a focused converted test red with a temporary grant-less handler write, revert it, then run its
focused green test and `just test-verbose tests/jobs/handlers`. Run `just lint`, `just type`, and
the full `just ci` gate before handoff.
