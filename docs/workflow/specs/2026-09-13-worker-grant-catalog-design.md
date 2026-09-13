# Worker grant catalog guard

Issue: [#2349](https://github.com/randomparity/kdive/issues/2349). Decision:
[ADR-0653](../../adr/0653-worker-grant-catalog-guard.md). Plan:
[2026-09-13-worker-grant-catalog.md](../plans/2026-09-13-worker-grant-catalog.md).

## Scope

Extend the existing #2347 handler-test inventory as the sole declaration for this guard. Its
existing module classifications remain intact; format version 2 adds the smallest explicit
worker write-coverage entries needed to exercise the migrated catalog. The guard covers only
entries declared in that inventory. It does not consume or replace the broader #2345 write
baseline, and it neither hides nor remediates that baseline's image-catalog leak.

## Decision

Each declared entry identifies a handler write, table, SQL write privilege, and one coverage
route. A `direct` route is covered only when the live migrated catalog reports
`has_table_privilege('kdive_worker', table, privilege)`. A `security-definer` route names an
exact function signature and is covered only when that catalog function exists, has
`pg_proc.prosecdef`, and gives `kdive_worker` `EXECUTE`. The latter deliberately does not
require a table grant: ADR-0629 keeps the underlying obligation table SELECT-only.

The schema test reads the committed inventory, validates the new declaration shape, and runs the
appropriate catalog predicate for every entry. Its diagnostic names both lawful remedies and
states that ADR-0629 prefers a fenced SECURITY DEFINER function when a table write must remain
fenced. It does not suggest widening a table grant for a definer-mediated entry.

## Failure and security model

The only changed trust boundary is the test's observation of existing PostgreSQL privilege
metadata. The input is repository-owned JSON committed with the test; validation restricts route,
table, privilege, and function-signature forms before queries bind their values as parameters.
The test uses the migration-owner fixture solely to inspect the catalog. It makes no production
role, schema, handler, migration, or grant change.

A missing direct grant fails with the declared handler-write identity. A missing function, a
non-definer function, or a missing worker EXECUTE grant fails with the same identity and function
signature. Controlled test-only revocations prove that the direct-table and definer-execute arms
both fail, with PostgreSQL transaction rollback restoring the migrated fixture's grants.

## Acceptance criteria

1. `worker_role_inventory.json` is the sole declared input for this guard and retains its #2347
   module inventory.
2. Every declared direct entry is checked with `has_table_privilege` against a migrated database.
3. Every declared definer entry is checked for existence, `prosecdef`, and worker `EXECUTE`.
4. A test-only direct-grant revoke and a test-only function-EXECUTE revoke each make the relevant
   arm red before rollback; the committed catalog passes.
5. The worker-only declared boundary and the unrelated #2345 image-catalog leak are explicit in
   the test and decision record.

## Verification

Run the focused schema test while proving both temporary revocations make it fail, then run it
clean. Run `just lint`, `just type`, the related inventory test, and the repository gate.
