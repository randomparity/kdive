# Handler worker-role tests design

## Scope

Issue #2347 converts job-handler test execution from the migration-owner connection to the shared
`kdive_worker` LOGIN pool. Owner access remains limited to seeding and observing database state.
The work changes tests and test documentation only; it does not alter runtime grants, production
handlers, schema, or platform error translation.

## Decision

[ADR-0651](../../adr/0651-handler-tests-use-worker-role-pools.md) owns the boundary: one LOGIN
set is created per xdist worker session under the existing maintenance-database role lock, while a
function-scoped pool connects that principal to each current migrated database. The same lock
protects teardown. A converted test passes `kdive_worker_pool` to the handler act phase or opens
an equivalent function-scoped pool from the role DSN, while fixture setup and postcondition reads
remain on owner connections. High-volume legacy modules bind that DSN in a test-local fixture so
their shared act helpers cannot retain an owner-execution fallback.

`tests/jobs/handlers/worker_role_inventory.json` classifies every one of the 34 handler test
modules. A `worker-act` record names each handler-act source location; `owner-only` records name
the source-backed reason no handler act connection exists; package fixtures are separately marked.
Its structural test rejects a missing module, duplicate path, invalid class, or an owner-backed
act source. Files that only use `migrated_url` to seed, inspect, or test non-handler helpers remain
owner-only with that explicit evidence.

## Acceptance criteria

1. Converted handler calls use the real worker LOGIN pool; setup and assertion reads still use the
   owner connection where the worker lacks the required privilege.
2. A controlled grant-less handler write raises raw `psycopg.errors.InsufficientPrivilege` through
   a converted handler test, and the controlled fault is removed before delivery.
3. The checked inventory covers all 34 handler test modules and names every converted worker act
   path or a source-backed owner-only reason.
4. The handler test tree passes in its normal xdist topology. The PR records before/after timings
   and creates/drops LOGIN roles once per xdist worker session, never per test.
5. Any newly discovered production grant leak parks this branch with its failing evidence and is
   reported, not repaired in this PR.
6. Focused fixture tests prove role lifecycle uses the cluster-global role lock for both creation
   and teardown.

## Failure handling

An `InsufficientPrivilege` raised by a converted test is evidence of a missing production grant.
This issue does not widen grants or map the exception. The run records the failing handler, SQL
operation, and test, then parks before handoff so no red test is represented as mergeable. The
session fixture drops its LOGIN principals when that xdist worker exits; each function-scoped pool
closes at test completion.

## Verification

Run a focused converted test red with a temporary grant-less handler write, revert it, then run its
focused green test and `just test-verbose tests/jobs/handlers`. Run `just lint`, `just type`, and
the full `just ci` gate before handoff.
