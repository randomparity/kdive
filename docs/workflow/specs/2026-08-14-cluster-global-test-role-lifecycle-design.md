# Cluster-global test role lifecycle design

## Goal

Make parallel migration tests deterministic when xdist workers use separate databases on one
PostgreSQL cluster, while preserving the production runtime-role shape and final privileges.

This design implements issue #1961 and is governed by
[ADR-0560](../../adr/0560-establish-runtime-role-dependency-before-validation.md).

## Scope and constraints

- Python 3.14 and PostgreSQL 17 remain the test/runtime versions.
- The host is `x86_64`; the project targets `x86_64` and `ppc64le`.
- Preserve canonical runtime role names and migration 0104 semantics.
- Preserve PostgreSQL transactionality; add no dependency or configuration.
- Keep migration 0104 byte-immutable; close its cross-database window in the runner before it
  executes.
- The full repository guardrail is `just ci`; CI gates its constituent recipes individually.

## Evidence and cause

Each xdist worker receives a separate `kdive_test_*` database, but all workers share the same
PostgreSQL server. Migration 0104 creates `kdive_server`, `kdive_worker`, `kdive_reconciler`, and
`kdive_lifecycle_witness`, which are cluster-global. The migration runner's advisory lock orders
connections only within one database. The `pg_conn` fixture drops and recreates `public`, and its
consumers apply partial or complete migrations and temporarily alter role attributes. A worker can
therefore manipulate the shared role lifecycle while a different worker migrates its database.

The original failure was intermittent: one full `just ci` produced 92 setup errors at migration
0104's `GRANT ... TO kdive_server`, while an unchanged rerun passed. Five current 16-worker focused
runs also passed. A controlled two-database experiment reproduced the exact error: migration 0104
paused after validating a shared role but before granting it; `DROP ROLE` from the other database
succeeded; and the resumed migration raised `UndefinedObject` at the grant. The current regression
pauses after the entire `DO` block, when the grant already exists, and therefore cannot detect the
window its name claims to cover.

## Design

`migrate.apply_migrations` detects a pending version 0104 immediately before executing its unchanged
SQL. A narrow helper attempts `GRANT USAGE ON SCHEMA public` for the four canonical runtime roles.
It ignores only `psycopg.errors.UndefinedObject`; every other database error propagates. The helper
accepts a role-name tuple solely so the deterministic regression can use isolated names, while the
production call passes one fixed module-level tuple.

The helper runs inside the same transaction and after the migration advisory lock is acquired. For
an existing compatible role, `GRANT` creates the dependency that blocks a concurrent drop before
0104 validates it. If a drop completes first, the helper observes an absent role and unchanged 0104
follows its safe create-then-grant path. For an incompatible role, 0104's validation error rolls the
provisional grant back without exposing it. The runner invokes no precondition when 0104 is already
recorded, and its checksum behavior is unchanged.

## Failure handling

Migration 0104 keeps the existing fail-closed behavior for a role with login, inheritance,
privileged attributes, or memberships. Because the provisional grant is transactional, that error
leaves neither the migration record nor the grant behind. A role dropped before the first grant is
recreated with the exact required shape; a role whose drop loses to the grant remains protected by
the database dependency.

## Verification

- Replace the misleading same-database regression with a two-database test that executes the narrow
  runner precondition and migration 0104's real bytes after mechanical role-name substitution. No
  test-only copy of 0104's role algorithm is permitted.
- Its old-order control skips the precondition, pauses unchanged 0104 after validation, completes a
  drop from the other database, and must reproduce `UndefinedObject` at `GRANT`.
- Its drop-wins fixed arm completes the cross-database drop before the precondition grant, then
  proves unchanged 0104 recreates and grants the role and leaves its exact required shape and
  schema dependency.
- Its grant-wins fixed arm lets the precondition grant establish the dependency first, then proves
  the cross-database drop blocks and fails with `DependentObjectsStillExist` while 0104 completes.
- The incompatible-role tests continue to prove that provisional grants roll back and that the
  pre-existing role and its privileges remain unchanged after rejection.
- Focused migration and runtime-role tests pass five consecutive times with 16 xdist workers and no
  retries.
- Three consecutive bare `just ci` runs pass from a clean tracked tree, with no setup errors,
  failures, or retrying an unchanged failed run. Each run uses the recipe's configured xdist
  parallelism; all three must pass before completion is claimed.

## Alternatives

ADR-0560 records the rejected fixture serialization, maintenance-database locking, per-worker role
rewriting, historical migration edit, forward-only repair, shorter-window, and do-nothing designs.
The pending-version runner precondition is the smallest immutable-compatible production fix.

## Durable workflow context

- Branch: `feat/stabilize-runtime-roles-1961`
- Base branch: `main`
- Guardrails: focused pytest commands during TDD; `just ci` before completion
- Architecture: host `x86_64`; targets `x86_64` and `ppc64le`; relationship `included`
