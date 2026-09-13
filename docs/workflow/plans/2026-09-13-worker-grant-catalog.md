# Plan: guard declared worker writes against the migrated catalog

## Goal

Implement #2349 without adding a second write-set artifact: extend the #2347 inventory and use
it to check declared worker write authority in a migrated PostgreSQL catalog.

## Task 1: Extend and validate the inventory

**Files:** `tests/jobs/handlers/worker_role_inventory.json`,
`tests/jobs/test_worker_role_inventory.py`.

Move the inventory to format version 2. Preserve every module classification and add the minimal
declared worker write-coverage list. Each list entry is uniquely identified and has either a
direct `(table, privilege)` route or an exact SECURITY DEFINER function signature. Reject missing,
duplicate, malformed, or unsupported coverage declarations in focused structural tests.

**Proof:** mutate a copied inventory to remove or duplicate a coverage entry and observe the
focused inventory test fail; restore it and observe green.

## Task 2: Query live catalog authority

**Files:** create a focused `tests/jobs/` migrated-catalog schema test.

Load the same inventory. For direct entries use `has_table_privilege`; for definer entries resolve
the bound function signature through `pg_proc` and assert function existence, `prosecdef`, and
`has_function_privilege` for `kdive_worker`. Keep all identifiers as validated data and query
parameters. Emit a diagnostic that explains direct grant and lawful function coverage without
prescribing a table grant for ADR-0629's fenced case.

**Proof:** in independent transactional test arms revoke one direct table grant and one exact
function EXECUTE grant, assert the relevant catalog check fails, then roll back. The committed
inventory passes against `migrated_url`.

## Task 3: Finish records and verify

**Files:** `docs/adr/0653-worker-grant-catalog-guard.md` and this plan's linked spec.

Set ADR-0653 Accepted after the implementation and focused proofs are green. Run the focused
tests, `just lint`, `just type`, and `just ci`. Do not edit production code, grants, migrations,
the #2345 baseline, or declarations for server and reconciler roles.

## Rollback

Reverting this test-only inventory extension, schema test, and ADR returns the previous absence
of the guard. No persistent database state needs cleanup.
