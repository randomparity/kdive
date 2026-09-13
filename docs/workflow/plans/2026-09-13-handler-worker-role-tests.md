# Handler worker-role tests implementation plan

## Goal

Implement issue #2347 using the shared worker LOGIN fixture from #2346 without changing production
database grants or handler behavior.

## Task 1 — inventory and conversion boundary

Identify every handler-test file that opens `migrated_url` as the pool supplied to a job handler.
For each, preserve owner setup and assertions while passing `kdive_worker_pool` to the act phase.
Keep the worker fixture function-scoped; do not create additional roles.

**Verification:** focused structural search confirms no converted handler invocation receives an
owner-backed pool; a focused file test proves its handler succeeds with seeded owner data.

## Task 2 — privilege regression proof

Add one focused handler regression that invokes a write lacking `kdive_worker` permission and
asserts raw `InsufficientPrivilege`. Make the controlled fault red, revert it, and retain the green
test against the normal schema.

**Verification:** the controlled fault fails the focused test with `InsufficientPrivilege`; after
reversion the exact test passes.

## Task 3 — suite proof and records

Run the handler tree with the normal test recipe topology, compare the measured wall time with the
pre-conversion run, update ADR-0651 to Accepted once the implementation PR is complete, and run the
repository gates.

**Verification:** `just test-verbose tests/jobs/handlers`, `just lint`, `just type`, and `just ci`
pass. ADR status guard accepts the final record.
