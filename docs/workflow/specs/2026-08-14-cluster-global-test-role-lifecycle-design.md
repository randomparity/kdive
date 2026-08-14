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
- Fix migration 0104's cross-database ordering rather than serializing the test suite.
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

Migration 0104 changes the order for each canonical role:

1. Attempt `GRANT USAGE ON SCHEMA public TO <role>` as the first role-specific operation.
2. On `undefined_object`, create the exact required role. Continue to tolerate
   `unique_violation` or `duplicate_object` from a concurrent creator.
3. Retry the same grant after the create-or-concurrent-create path.
4. Query the role attributes and memberships and raise the existing incompatibility error unless
   they match exactly.

For an existing compatible role, `GRANT` either wins and creates the dependency that blocks a
concurrent drop, or loses to a completed drop and enters the create path. For an absent role, the
creating transaction prevents a concurrent drop before its grant. For an incompatible role, the
grant and later validation error share one transaction, so rollback removes the provisional ACL
without exposing it to other sessions.

## Failure handling

The migration keeps the existing fail-closed behavior for a role with login, inheritance,
privileged attributes, or memberships. Because the provisional grant is transactional, that error
leaves neither the migration record nor the grant behind. A role dropped before the first grant is
recreated with the exact required shape; a role whose drop loses to the grant remains protected by
the database dependency.

## Verification

- Replace the misleading same-database regression with a two-database test using isolated runtime
  role names. Its unlocked control pauses after validation, drops the role from the other database,
  and must reproduce `UndefinedObject` at `GRANT`.
- The fixed-path arm executes the dependency-first migration. Once the grant operation begins, the
  cross-database drop must block and then fail with `DependentObjectsStillExist`; migration must
  complete successfully.
- The incompatible-role tests continue to prove that provisional grants roll back and that the
  pre-existing role and its privileges remain unchanged after rejection.
- Focused migration and runtime-role tests pass five consecutive times with 16 xdist workers and no
  retries.
- Three consecutive bare `just ci` runs pass from a clean tracked tree, with no setup errors,
  failures, or retrying an unchanged failed run. Each run uses the recipe's configured xdist
  parallelism; all three must pass before completion is claimed.

## Alternatives

ADR-0560 records the rejected fixture serialization, maintenance-database locking, per-worker role
rewriting, shorter-window, and do-nothing designs. Dependency-first ordering is the smallest change
that makes the database operation itself safe in both tests and production.

## Durable workflow context

- Branch: `feat/stabilize-runtime-roles-1961`
- Base branch: `main`
- Guardrails: focused pytest commands during TDD; `just ci` before completion
- Architecture: host `x86_64`; targets `x86_64` and `ppc64le`; relationship `included`
