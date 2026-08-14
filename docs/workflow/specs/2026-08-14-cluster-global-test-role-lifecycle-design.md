# Cluster-global test role lifecycle design

## Goal

Make parallel migration tests deterministic when xdist workers use separate databases on one
disposable PostgreSQL cluster, without changing production migration or runtime-role behavior.

This design implements issue #1961 and is governed by
[ADR-0560](../../adr/0560-serialize-cluster-global-test-role-lifecycle.md).

## Scope and constraints

- Python 3.14 and PostgreSQL 17 remain the test/runtime versions.
- The host is `x86_64`; the project targets `x86_64` and `ppc64le`.
- Preserve canonical runtime role names and migration 0104 semantics.
- Use the existing pytest per-run root and `fcntl.flock`; add no dependency or configuration.
- Serialize only fixture paths capable of changing cluster-global role state.
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
runs also passed. The absence of a deterministic failure under ordinary scheduling is consistent
with, rather than contrary to, an unguarded cross-worker ordering. Repository search found no
production or test path that intentionally drops a canonical role; the unsafe condition is that
the fixture provides no ownership boundary for any cluster-global role operation.

## Design

`tests.support.xdist_backend` exposes a context manager for a named run-scoped exclusive lock. It
derives a lock file below `per_run_root(tmp_path_factory)` and delegates to the module's existing
kernel `flock` primitive. The name is fixed for PostgreSQL cluster-global state, and the API stays
generic enough to describe the resource being guarded without introducing a lock registry.

`pg_conn` acquires the PostgreSQL cluster-global lock before dropping `public`. It holds the lock
across `yield`, so direct migration SQL, role alteration, and teardown performed by the consuming
test remain inside the boundary. `_migrated_db` acquires the same lock around its one-time
`apply_migrations` call and post-migration snapshot. It releases the lock before yielding the
already-migrated URL, allowing ordinary service tests to retain xdist parallelism.

The lock is process-owned by `flock`. Normal fixture exit and abrupt process termination both
release it. A waiting worker blocks on the state transition it needs instead of sleeping or retrying
on a guessed interval.

## Failure handling

Failure while the lock is held propagates unchanged after Python unwinds the context manager. No
state file or manual cleanup is required. A missing or unusable per-run root fails the fixture at
lock acquisition rather than running without serialization.

The fixture does not attempt to repair a role that another process removed. Its contract prevents
supported test paths from overlapping; unexpected external cluster mutation remains a loud
migration failure.

## Verification

- A cross-process unit test acquires the named lock in one process, proves a contender cannot enter
  until release, then proves it enters after release.
- A fixture test proves `pg_conn` holds the named lock throughout the consuming test, not only while
  resetting the schema.
- A fixture test proves `_migrated_db` uses the same named boundary while migrations execute and
  does not retain it while ordinary migrated tests run.
- Focused migration and runtime-role tests run with 16 xdist workers repeatedly.
- `just ci` passes from a clean tracked tree.

## Alternatives

ADR-0560 records the rejected per-worker role rewriting, pre-creation, production-lock change, and
call-only lock designs. The selected fixture-lifetime boundary is the smallest surface that covers
direct SQL and role mutation while leaving already-migrated tests parallel.

## Durable workflow context

- Branch: `feat/stabilize-runtime-roles-1961`
- Base branch: `main`
- Guardrails: focused pytest commands during TDD; `just ci` before completion
- Architecture: host `x86_64`; targets `x86_64` and `ppc64le`; relationship `included`
