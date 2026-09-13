# Handler worker-role tests implementation plan

## Goal

Implement issue #2347 using the shared worker LOGIN fixture from #2346 without changing production
database grants or handler behavior.

## Task 1 — inventory and conversion boundary

Create a session-scoped, xdist-worker-local LOGIN set after migrations and a function-scoped DSN
adapter/pool for each current migrated database. Identify every handler-test module and add a
checked `worker_role_inventory.json` record: worker-act records cite every handler-act source
location; owner-only records cite the source-backed reason; package fixtures are marked directly.
For each worker-act path, preserve owner setup and assertions while passing `kdive_worker_pool` to
the act phase. Do not create or drop a LOGIN principal per test.

**Verification:** the structural inventory test rejects a missing module, duplicate path, invalid
classification, and owner-backed worker-act source; a focused file test proves its handler succeeds
with owner-seeded data.

## Task 2 — privilege regression proof

Add one focused handler regression that invokes a write lacking `kdive_worker` permission and
asserts raw `InsufficientPrivilege`. Make the controlled fault red, revert it, and retain the green
test against the normal schema. An unrecorded real grant failure parks this branch with its exact
test, handler, and SQL-operation evidence; it is not fixed or treated as merge-ready here.

**Verification:** the controlled fault fails the focused test with `InsufficientPrivilege`; after
reversion the exact test passes.

## Task 3 — suite proof and records

Run the handler tree with the normal test recipe topology, compare the measured wall time with the
pre-conversion run, update ADR-0651 to Accepted once the implementation PR is complete, and run the
repository gates.

**Verification:** `just test-verbose tests/jobs/handlers`, `just lint`, `just type`, and `just ci`
pass. ADR status guard accepts the final record.
