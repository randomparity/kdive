# 0560 — Serialize cluster-global test role lifecycle

## Status

Accepted (2026-08-14)

## Context

The parallel database suite gives each pytest-xdist worker separate databases on one shared
PostgreSQL cluster. Database schemas are isolated, but the four KDIVE runtime roles created by
migration 0104 are cluster-global. Migration-focused tests rebuild schemas, execute partial or
complete migrations, and temporarily alter role state while other workers may migrate.

A deterministic two-database reproduction pauses migration 0104 after validating a shared role but
before its first grant. `DROP ROLE` from the other database then succeeds because the role has no
dependency in the migrating database. When migration resumes, `GRANT USAGE ... TO kdive_server`
fails with `UndefinedObject`, matching the full-suite failure. The existing same-database regression
pauses after the grant and cannot exercise this window.

The reproduction establishes why test operations need one cluster-global lifecycle owner. It does
not establish that supported production processes drop canonical runtime roles, so changing the
production migration runner would exceed this issue's test-lifecycle authority.

## Decision

The disposable-Postgres fixture serializes every test operation that can change the lifecycle or
shape of cluster-global runtime roles with one PostgreSQL session advisory lock. Every contender
connects to the shared `postgres` maintenance database before taking the lock. PostgreSQL scopes an
advisory lock to its database, so this common connection identity coordinates xdist workers and
independently rooted test runs that share the same server.

A test using `pg_conn` holds the guard connection and lock from before its schema reset until after
the consuming test and fixture teardown. Creation of each worker's session-scoped migrated database
holds the same lock while applying migrations and capturing its seed snapshot, then releases it
before yielding the already-migrated URL. Those two paths cover every direct migration test found in
the repository while leaving ordinary migrated-database tests parallel.

The fixture reuses ADR-0015's two-integer migration advisory-lock key through the maintenance
database. It creates no second lock namespace and changes no production behavior or migration file.

A deterministic guarded counterpart uses the actual maintenance-database helper around the same
two-database lifecycle as the vulnerable control. It records the helper connection's backend PID
and proves that exact backend is waiting on the maintenance-database key before releasing the first
actor. The control continues to reproduce `UndefinedObject` when it intentionally bypasses the
guard, so a mismatched database, key, late acquisition, or early release makes the guarded test
fail instead of relying on stress frequency.

Each acquisition is bounded to 60 seconds of PostgreSQL server elapsed time, scoped to one lock
attempt. On expiry the fixture fails before entering the protected operation and reports the
maintenance database, lock key, visible blocking backend identifiers, and recovery action: stop the
stuck test worker or let it exit, then rerun the failed test. The fixture never steals a lock from a
live holder.

## Consequences

Migration-focused tests using `pg_conn` run serially across workers and concurrent runs sharing the
server. Tests using an already-migrated database remain parallel after each worker's one-time
migration. The exact serialization cost is not yet measured; it is bounded to migration-focused
fixture lifetimes and accepted because the issue requires a deterministic full suite.

Normal fixture exit and process termination close the guard connection, so PostgreSQL releases the
session lock. A live hung holder can delay contenders for at most the 60-second per-acquisition
server-clock interval before they fail with diagnostics; operator recovery is still required for
that holder.

## Considered & rejected

- **Change migration 0104 or the production runner.** The reproduction requires a test actor to
  drop a shared role and does not prove a supported production actor does so. Migration 0104 is also
  byte-immutable under ADR-0015.
- **Use a run-scoped filesystem lock.** Different pytest runs have different per-run roots but may
  share an override PostgreSQL server, leaving their cluster-global roles unordered.
- **Rename runtime roles per worker.** Every authority migration and role-based connection would
  need rewriting, making tests exercise names and assembly unlike production.
- **Lock only `apply_migrations` calls.** Tests execute migration SQL and mutate role attributes
  after `pg_conn` yields, so call-level locking leaves the reproduced lifecycle unordered.
- **Do nothing.** One full suite failed with 92 setup errors, and the deterministic two-database
  control reproduces the same `UndefinedObject` at `GRANT`. Retaining the race violates the issue's
  deterministic-suite criterion.
