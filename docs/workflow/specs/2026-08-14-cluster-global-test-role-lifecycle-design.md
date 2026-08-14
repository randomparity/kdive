# Cluster-global test role lifecycle design

## Goal

Make parallel migration tests deterministic when xdist workers use separate databases on one
PostgreSQL cluster, while preserving the production runtime-role shape and final privileges.

This design implements issue #1961 and is governed by
[ADR-0560](../../adr/0560-serialize-cluster-global-test-role-lifecycle.md).

## Scope and constraints

- Python 3.14 and PostgreSQL 17 remain the test/runtime versions.
- The host is `x86_64`; the project targets `x86_64` and `ppc64le`.
- Preserve canonical runtime role names and migration 0104 semantics.
- Preserve PostgreSQL transactionality; add no dependency or configuration.
- Keep migration 0104 and the production migration runner unchanged.
- Serialize only fixture lifetimes that can manipulate cluster-global role state.
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

`tests/db/conftest.py` defines `_cluster_global_role_lock(postgres_url, *, timeout_ms=60_000)`. It
derives the common `postgres` maintenance-database URL, opens a dedicated autocommit connection,
sets the acquisition statement timeout, and takes a session advisory lock using ADR-0015's existing
two-integer migration key. The context manager closes the connection in an outer `finally`.
Session close is the sole release mechanism; the helper does not issue a separate unlock query
whose failure could replace a consuming-test exception.

`pg_conn` enters the context before dropping and recreating `public`, then holds it across `yield`.
The consuming test's partial migrations, direct 0104 execution, role mutation, and cleanup therefore
share the boundary. `_migrated_db` enters the same context around its one-time `apply_migrations`
and snapshot capture, then releases it before yielding ordinary migrated access.

Repository inventory shows direct migration execution uses `pg_conn`; the once-per-worker migrated
path is the other migration entry during the parallel suite. Tests that use an already-migrated URL
do not change role lifecycle and need no lock. The admin migration test directly resets and migrates
`postgres_url`; it explicitly enters the same helper around that complete operation.

## Failure handling

The lock query uses a 60-second `statement_timeout`: seconds are measured by the PostgreSQL server's
elapsed clock, and the limit applies per acquisition. Timeout prevents entry and raises an
actionable fixture error naming the maintenance database, key, visible blockers, and recovery
action. Other connection or database errors propagate. Context exit closes the dedicated session in
an outer `finally`, releasing the lock without issuing another database operation and without
replacing a consuming-test exception.

## Verification

- A two-database control mechanically substitutes isolated names into real 0104 bytes, pauses after
  validation and before grant, completes a cross-database drop, and reproduces `UndefinedObject`.
- The fixed integration holds `_cluster_global_role_lock` around that migration lifecycle, starts
  the competing drop from a second process that must acquire the same helper first, proves it cannot
  enter until release, then proves both operations finish in order.
- Fixture-wiring tests prove `pg_conn` holds the helper across the consuming test and teardown, and
  `_migrated_db` holds it around migration and snapshot but not ordinary migrated access.
- The admin migration test instruments the same helper and proves acquisition precedes its direct
  schema reset and remains held through `migrate(postgres_url)` and the post-migration assertion.
- A timeout test uses a shortened test-only timeout and checks the complete diagnostic and recovery
  contract while another maintenance-database session owns the real key.
- Focused migration and runtime-role tests pass five consecutive times with 16 xdist workers and no
  retries.
- Three consecutive bare `just ci` runs pass from a clean tracked tree, with no setup errors,
  failures, or retrying an unchanged failed run. Each run uses the recipe's configured xdist
  parallelism; all three must pass before completion is claimed.

## Alternatives

ADR-0560 records the rejected production change, per-run filesystem lock, per-worker role rewriting,
call-only lock, and do-nothing designs. The maintenance-database fixture boundary is the smallest
surface covering every identified test role-lifecycle path.

## Durable workflow context

- Branch: `feat/stabilize-runtime-roles-1961`
- Base branch: `main`
- Guardrails: focused pytest commands during TDD; `just ci` before completion
- Architecture: host `x86_64`; targets `x86_64` and `ppc64le`; relationship `included`
