# 0560 — Serialize cluster-global test role lifecycle

## Status

Proposed

## Context

The parallel database suite gives each pytest-xdist worker its own database on one shared
PostgreSQL cluster. Database schemas are isolated, but the four KDIVE runtime roles created by
migration 0104 are cluster-global. Migration advisory locks are scoped to a database and therefore
do not order migrations in different worker databases. Migration-focused tests also rebuild their
schema or temporarily alter roles while other workers may be applying migrations. One full suite
observed migration 0104 reach a grant after `kdive_server` disappeared; an unchanged rerun and
focused stress runs passed, demonstrating an ordering-dependent shared-state defect.

## Decision

The disposable-Postgres fixture serializes every test operation that can change the lifecycle or
shape of cluster-global runtime roles with one PostgreSQL session advisory lock. Every contender
connects to the shared `postgres` maintenance database before taking the lock, rather than taking
it through its worker database. The common database identity makes the boundary cluster-wide for
all workers and concurrent test runs that use the same server. A test using `pg_conn` holds the
guard connection and lock from before its schema reset until after the test and fixture teardown.
Creation of each worker's session-scoped migrated database holds the same lock while applying
migrations and capturing its seed snapshot.

The lock uses a dedicated two-integer advisory-lock key reserved by the test fixture. It is a
test-infrastructure boundary only. Production migration behavior, runtime role names, and
per-database migration locking remain unchanged.

## Consequences

Migration-focused tests that use `pg_conn` run serially across xdist workers and concurrent runs
sharing the server. Tests using an already-migrated database remain parallel after the
once-per-worker migration completes. A process exit closes its guard connection and PostgreSQL
releases the session lock, so an interrupted worker cannot permanently wedge the suite. The added
serialization cost is not yet measured; it is bounded to migration-focused fixture lifetimes and
is accepted because the issue requires deterministic full-suite runs.

The lock is intentionally broader than migration 0104: a test can execute migrations directly or
temporarily alter a canonical role after the fixture yields, so locking only `apply_migrations`
would leave those paths unordered. New fixtures that manipulate cluster-global PostgreSQL objects
must join this boundary.

## Considered & rejected

- **Rename runtime roles per worker.** Every authority migration and role-based connection would
  need rewriting. That would make the tests exercise names and assembly unlike production.
- **Create the canonical roles once before worker databases.** This duplicates migration 0104's
  role shape and ownership in fixture code and still leaves tests that temporarily alter roles.
- **Change the production migration lock.** The reported defect is shared disposable-test state,
  and PostgreSQL advisory locks taken through different databases do not provide the required
  cluster boundary. Adding an external production coordinator is outside this issue.
- **Lock only calls to `apply_migrations`.** Some tests apply migration SQL directly or mutate role
  attributes after the fixture yields, so call-level locking does not cover the reproduced hazard.
- **Use a run-scoped filesystem lock.** Different pytest runs have different per-run roots but may
  share an override PostgreSQL server, leaving their cluster-global roles unordered.
- **Do nothing.** One full suite already failed with 92 setup errors and an unchanged rerun passed.
  Keeping that race would violate the issue's deterministic-suite criterion even though five
  focused stress runs did not reproduce it. The exact parallelism cost of serialization is not yet
  measured, but retaining an observed nondeterministic gate is not acceptable.
