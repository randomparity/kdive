# ADR 0419 — Migrate once per worker, reset db-backed tests by TRUNCATE + snapshot restore

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-21
- **Deciders:** D. Christensen (core platform)

## Context

`tests/db/conftest.py`'s `migrated_url` fixture (~185 dependent test modules) reset
its database for every single test by dropping and recreating the `public` schema
(`pg_conn`), then calling `migrate.apply_migrations` to replay all ~72 migration SQL
files from scratch (#1333). Measured at ~60ms/test, this is paid on every db-backed
test — thousands of times per run — even though the schema produced is byte-for-byte
identical every time: no test in the fast-path population mutates the schema itself,
only the rows in it.

ADR-0401 (#1331) landed the prerequisite this issue depends on: one Postgres
container per run, with each xdist worker owning its own `kdive_test_<worker>_<token>`
database (`postgres_url`, session-scoped) rather than its own container. That gives a
stable per-worker database to migrate once and reuse, instead of once per test.

A separate population of tests in `tests/db/` genuinely needs the *real* migration
runner to run incrementally — `test_migrate.py` and the per-migration-number tests
(`test_migration_00NN_*.py`, `test_artifacts_run_id_migration.py`,
`test_run_system_label_migration.py`, `test_investigation_cleanup_marker.py`,
`test_resource_kind_parity.py`, `test_image_catalog_migration.py`) construct partial
or staged schema states on purpose — e.g. `_apply_through(pg_conn, "0029")` to assert
a pre-migration invariant, then apply the rest and assert the post-migration one.
These tests all consume `pg_conn` directly (verified by grep: no test outside
`tests/db/` requests `pg_conn`, and every `tests/db/` module requests either
`pg_conn` or `migrated_url`, never both) and must keep their drop+remigrate
semantics unchanged.

A handful of migrations also seed reference data via `INSERT INTO` in the migration
SQL itself (`system_shapes` in 0013/0061, `cost_class_coefficients` in 0002/0032,
`ops_control` in 0011, `build_hosts` in 0027) — rows that exist only because
`apply_migrations` ran once. A naive `TRUNCATE` reset would discard them permanently
after the first test, since migrations never run again on the fast path (confirmed
by an initial `TRUNCATE`-only spike: `test_repositories.py::test_seed_coefficient_is_readable`
failed on the second test to touch `cost_class_coefficients` in a worker, because the
first test's implicit reset had already wiped the seeded `('local', 1.0)` row with no
migration left to re-insert it).

## Decision

Add a second per-worker database migrated exactly once, and reset it per test with a
snapshot/restore step around `TRUNCATE` so seed data introduced by migrations
survives every reset.

1. **Two databases per worker, not one.** `postgres_url` is unchanged: still one
   worker database, still backing `pg_conn`'s drop+recreate path exactly as before. A
   new session-scoped `_migrated_db` fixture provisions a **second**, separate worker
   database — deriving the shared server from `postgres_url`'s own conninfo
   (`_server_url_without_db`) rather than re-acquiring the container, so this costs
   one more `CREATE DATABASE` plus one migration replay per worker, not a second
   container acquisition — migrates it exactly once via `migrate.apply_migrations`,
   and snapshots every application table's rows immediately after. Depending on
   `postgres_url` also nests `_migrated_db`'s teardown inside it, so the shared server
   is still alive when the second database is dropped. Keeping these as two physical
   databases — rather than one database serving both fixtures — means `pg_conn`'s
   schema drop can never run against the database `migrated_url` assumes is already
   migrated, regardless of test execution order within a worker.

2. **Per-test reset is `TRUNCATE ... RESTART IDENTITY CASCADE` plus a snapshot
   restore, not a schema drop.** `_migrated_db`'s post-migration snapshot is captured
   with `COPY <table> TO STDOUT (FORMAT binary)` per table (excluding
   `schema_migrations`, whose bookkeeping rows do not matter once nothing calls
   `apply_migrations` again). Before each test, `migrated_url` truncates every
   application table, then restores the snapshot with `COPY <table> FROM STDIN
   (FORMAT binary)`. This is the "migrated once, reset per test" split the issue
   proposed, generalized so it also restores any seed rows a migration inserted —
   without a hard-coded list of which tables happen to carry seed data, so a future
   migration that adds more seed data is covered automatically rather than silently
   breaking the fast path again.

3. **The migration-runner tests are untouched.** `pg_conn` and `postgres_url` keep
   their exact prior behavior and fixture names; the migration-number tests and
   `test_migrate.py` were not touched at all.

4. **`_migrated_db` is re-exported alongside the existing three fixture names.**
   pytest resolves a fixture's own dependencies by name against the conftest
   hierarchy applicable to the *test currently running*, not against the module the
   fixture was originally defined in. The ~17 sibling `conftest.py` files outside
   `tests/db/` already re-export `migrated_url`, `pg_conn`, and `postgres_url` with
   `from tests.db.conftest import ...` so tests under their directories can request
   them; `migrated_url` now also depends on `_migrated_db`, so every one of those
   files needs the same name added to its import (and `__all__`, where present) or
   every test outside `tests/db/` fails collection with "fixture '_migrated_db' not
   found." This was caught by running the full suite, not just `tests/db`, before
   declaring the change done.

## Consequences

- `tests/db` full-suite wall time drops from ~38s (baseline: drop+remigrate per test)
  to ~27s locally with the same 246 tests passing — the majority of the recoverable
  time the issue estimated, with the remainder being fixed per-worker migration cost
  that no longer scales with test count.
- Every worker now provisions two databases instead of one. This does not change the
  `max_connections` sizing ADR-0401 established (that scales with worker count ×
  pool size, not database count); it is two extra lazily-created/dropped databases,
  not two extra hot connection pools.
- **Residual — a future migration that mutates existing rows in place (`UPDATE`, not
  `INSERT`) rather than seeding a fresh table is still covered**, since the whole
  table's post-migration bytes are snapshotted, not just newly-seeded rows.
- **Residual — a test that adds a *new* table via raw DDL on `migrated_url`'s
  connection** (rather than through a migration) would not be captured by the
  once-per-worker snapshot and would leak into the next test. No such test exists
  today (verified: the fast-path population never issues `CREATE TABLE`); this is a
  contract the migration-runner tests still hold, and any future DDL-on-`migrated_url`
  test should use `pg_conn` instead.
- The change touches `tests/db/conftest.py` plus the ~17 sibling `conftest.py` files
  that re-export its fixtures (one added name each, see Decision point 4); no
  production code, schema, or migration changed.

## Considered & rejected

- **`TRUNCATE` alone, with no snapshot/restore.** The natural first read of the
  issue's proposed fix. Rejected on evidence, not speculation: it silently discarded
  migration-seeded reference rows (`cost_class_coefficients`, `system_shapes`, …)
  after the first test to touch them in a given worker, failing
  `test_repositories.py::test_seed_coefficient_is_readable` in the first full run of
  this design. A reset must reproduce every observable effect of `apply_migrations`,
  not just its DDL.
- **Hard-code the known seed tables and re-run their literal `INSERT` statements in
  the fixture.** Would have fixed the immediate failure but duplicates seed data
  already expressed once in the migration SQL, and silently stops covering a table
  the moment a *new* migration adds seed data without a matching fixture update.
  Rejected in favor of a snapshot that is derived from whatever `apply_migrations`
  actually produced, so it can never drift from the migrations themselves.
- **`CREATE DATABASE ... TEMPLATE` per test**, the issue's other named option. Clones
  the whole database (schema, seed data, and sequence state) in one step with no
  snapshot bookkeeping. Rejected as the per-test mechanism: `CREATE DATABASE` takes an
  `ACCESS EXCLUSIVE`-equivalent lock and requires no other session connected to the
  template, which is a materially heavier and more failure-prone operation to run
  once *per test* than a `TRUNCATE` + row-level `COPY`, and would need a fresh
  database name (and matching teardown) every test rather than resetting one
  long-lived connection target.
- **One shared database for both `pg_conn` and `migrated_url`,** relying on test
  ordering to keep the schema-dropping migration-runner tests from running between a
  worker's fast-path tests. Rejected: pytest-xdist does not guarantee that ordering
  within a worker, and correctness must not depend on scheduling. Two independent
  per-worker databases make the fixtures correct regardless of interleaving.
- **A module-level cache plus a `pytest_sessionfinish` hook, instead of a proper
  session-scoped `_migrated_db` fixture,** to avoid touching the ~17 sibling
  `conftest.py` re-export files (hooks, unlike fixtures, are collected from every
  `conftest.py` project-wide, so this would have needed no cross-file changes).
  Rejected: session-scoped fixture teardown already runs in dependency order (a
  fixture tears down before the fixtures it depends on), which is exactly the
  ordering this needs (the second database must be dropped while the shared server
  `postgres_url` depends on is still up); `pytest_sessionfinish` fires once at the
  very end of the run with no guaranteed relationship to that per-fixture teardown
  order, making "is the server still alive when we try to drop the second database"
  an open question rather than a property `pytest` already guarantees. A real
  fixture dependency (`migrated_url` → `_migrated_db` → `postgres_url`) gets correct
  ordering for free; the mechanical one-name addition to 17 imports was the smaller
  risk.
