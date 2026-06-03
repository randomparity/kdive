# ADR 0015 — Forward-only SQL migration runner

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-03
- **Deciders:** D. Christensen (core platform)

## Context

[0005](0005-postgres-object-store-state.md) makes Postgres the system-of-record
for all structured state. Something has to create and evolve that schema, and the
M0 plan (`../plans/m0-implementation.md`, "Issue 4 — Postgres schema & migration
runner") pins a **minimal forward-only migration runner** that applies
`schema/NNNN_*.sql` in order, tracks what it applied in a `schema_migrations`
table, and is a no-op on re-run.

The forces that bound the mechanism:

- **Concurrent startup.** The server, worker, and reconciler are separate
  processes ([0014](0014-structured-logging.md) context). More than one may start
  and attempt to migrate the same database at once; two migrators must not both
  apply `0001`.
- **Atomicity.** A migration that fails partway must leave the database unchanged,
  not half-applied — otherwise the next startup sees an ambiguous state.
- **Applied migrations are immutable.** Forward-only means an already-applied file
  is never edited; if one is edited anyway (a developer mistake), the runner should
  fail loudly rather than silently diverge code from the live schema.
- **The schema is the system-of-record, and its value sets will grow.** The M0
  state machines are an explicit *subset* — the spec defers `reprovisioning` and
  DebugSession reattach to M1 (`../specs/m0-walking-skeleton.md`, "Non-goals").
  Whatever enforces the closed value sets at the database must be cheap to extend
  in a later forward-only migration.

This ADR pins the runner mechanism and the schema-encoding choices the plan and
the spec leave open; it does not restate the table list (the spec's "Postgres
schema (M0 subset)" pins that) nor re-argue Postgres-as-store
([0005](0005-postgres-object-store-state.md)).

## Decision

We will hand-write a forward-only runner over plain `.sql` files:

- **Plain, parameterless SQL files**, `schema/NNNN_*.sql`, applied in ascending
  `NNNN` order. No down-migrations: M0 has no production data, so rollback is
  dropping the disposable database. Each file is executed with a single
  parameterless `conn.execute(sql)` — psycopg 3 sends a parameterless string as one
  simple-query batch, so a file's multiple statements (and dollar-quoted function
  bodies) apply intact without client-side `;`-splitting.
- **One transaction for the whole pending set, guarded by a
  transaction-scoped advisory lock.** The runner opens a transaction, takes a
  single `pg_advisory_xact_lock(<fixed key>)`, creates `schema_migrations` if
  absent, reads the applied versions, applies every pending file in order, and
  commits. The lock serializes concurrent migrators; transaction scope means it
  auto-releases on commit/abort with no explicit unlock to leak — the same
  discipline [0005](0005-postgres-object-store-state.md) chose for per-Allocation
  and per-System locks. All-or-nothing: a failure aborts the transaction and the
  schema is untouched.
- **`schema_migrations(version, filename, checksum, applied_at)`** records each
  applied file. On every run the runner verifies that each already-applied
  version's recorded `checksum` (SHA-256 of the file bytes) still matches the file
  on disk, and raises if not — catching an edited-after-apply migration.
- **Closed value sets are `text` columns with named `CHECK` constraints**, not
  native `ENUM` types. The `CHECK` mirrors the corresponding
  `kdive.domain.state` / `kdive.domain.errors` enum and enforces the closed set at
  the system-of-record; a future milestone widens a set with
  `ALTER TABLE … DROP CONSTRAINT … ; ADD CONSTRAINT …` in a new migration.
- **`updated_at` is maintained by the database**, via one shared
  `set_updated_at()` `BEFORE UPDATE` trigger attached to every table that carries
  the column, so the timestamp is correct regardless of which writer (repository,
  reconciler, or a manual fix) performed the update.
- **`id uuid PRIMARY KEY DEFAULT gen_random_uuid()`** — `gen_random_uuid()` is in
  core Postgres since 13, so no extension is required; the minimum supported
  Postgres is **14**.

Tests run against a disposable Postgres via `testcontainers`; the fixture skips
with a clear reason when the Docker daemon is unreachable, so the suite stays green
for a contributor without Docker while running for real in CI (Docker is present on
the GitHub-hosted runner) and the MinIO/object-store sibling (Issue 6) uses the
same harness.

## Consequences

What becomes easier:

- A new schema change is a new numbered file; the runner needs no edit. Startup of
  any process converges the schema, and concurrent startups are safe.
- A partly-failed migration never leaves a half-applied schema — the run is atomic.
- An accidental edit to an applied migration fails fast with a checksum mismatch
  instead of silently drifting code from the live schema.
- Growing a state machine in M1 is a constraint swap in a forward migration, with
  none of native `ENUM`'s evolution friction.

What becomes harder / new obligations:

- **The single-transaction model forbids non-transactional DDL.** A future
  migration needing `CREATE INDEX CONCURRENTLY` or `VACUUM` cannot run inside the
  shared transaction and would force a documented per-file-transaction escape
  hatch. M0's schema uses none, so this is deferred, not solved.
- **The closed value sets now live in two places** — the Python enum and the SQL
  `CHECK` — and must stay in sync. A behavioral test (every enum value inserts;
  an off-list value is rejected) ties the SQL back to the enum so drift is caught.
- **A very long migration holds the advisory lock and a transaction** for its
  duration, briefly serializing startups. Acceptable for a startup-time operation;
  M0 migrations are schema DDL measured in milliseconds.
- Two dev-only dependencies are added: `psycopg-pool` (the async pool the plan
  names) and `testcontainers` (the disposable-Postgres harness).

## Alternatives considered

- **Alembic.** Rejected: its value is autogeneration from SQLAlchemy ORM metadata,
  and this project has no ORM — state is hand-written Pydantic over raw
  `psycopg`. Alembic would add a dependency and a migration environment heavier
  than a forward-only applier of `.sql` files needs, with no autogenerate benefit
  to draw on.
- **`yoyo-migrations` / `sqitch`.** Rejected: a third-party tool (or a Perl
  toolchain, for sqitch) for what is a short, auditable Python function over a
  directory of SQL.
- **Native Postgres `ENUM` types for the value sets.** Rejected on evolvability:
  `ALTER TYPE … ADD VALUE` cannot be used in the same transaction that adds it,
  values cannot be dropped or reordered, and the M0 state machines are a
  deliberately-growing subset. `text` + `CHECK` gives identical closed-set
  enforcement at the system-of-record with a plain `ALTER TABLE` to evolve it.
- **App-managed `updated_at`** (every writer sets it). Rejected: a writer that
  forgets silently leaves a stale timestamp, and the reconciler and audit trail key
  off it. A `BEFORE UPDATE` trigger makes correctness the default, not a per-writer
  obligation.
- **Per-file transaction plus a session-scoped advisory lock** held across the
  loop. Rejected for M0: it sacrifices all-or-nothing atomicity across the pending
  set and requires an explicit `pg_advisory_unlock` that leaks the lock if the
  process dies mid-loop. The single-transaction model is simpler and matches
  [0005](0005-postgres-object-store-state.md). It is the documented escape hatch if
  a future non-transactional migration ever needs it.
- **No checksum tracking.** Rejected: an edited applied-migration would diverge the
  live schema from source with no signal until a confusing downstream failure.
