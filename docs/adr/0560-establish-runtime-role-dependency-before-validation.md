# 0560 — Establish runtime-role dependency before validation

## Status

Proposed

## Context

Migration 0104 creates or validates four cluster-global KDIVE runtime roles. Each pytest-xdist
worker migrates a separate database on one shared PostgreSQL cluster. The migration currently
validates an existing role and only then grants schema usage to establish a dependency in its
database.

A deterministic two-database reproduction pauses migration 0104 after validation but before its
first grant. `DROP ROLE` from the other database then succeeds because the validated role has no
dependency in the migrating database. When migration resumes, `GRANT USAGE ... TO kdive_server`
fails with `UndefinedObject`. The existing same-database regression pauses after the grant, so it
does not exercise this cross-database window.

## Decision

Migration 0104 establishes the current database's schema dependency before it validates a runtime
role. For each role, it first attempts `GRANT USAGE ON SCHEMA public`. If PostgreSQL reports that
the role is undefined, the migration creates the exact non-login role, tolerates a concurrent exact
creator, and retries the grant. It then reads and validates the role's attributes and memberships.

The grant and validation remain in the migration transaction. If an existing role is incompatible,
the migration raises its existing error and PostgreSQL rolls back the provisional grant. An
uncommitted grant is not visible to other sessions. If a concurrent drop wins before the first
grant, the undefined-role path recreates and grants the required role. If the grant wins, its
dependency prevents the concurrent drop. Creation followed by grant is protected by the creating
transaction's shared-catalog lock.

The regression uses two databases and an isolated set of runtime-role names. It pauses immediately
before the dependency-first operation, starts a cross-database drop, and proves the drop cannot
complete after the grant begins. A companion unlocked control preserves the formerly vulnerable
validate-then-grant ordering and must reproduce the reported `UndefinedObject` at the grant.

## Consequences

Parallel migrations and cluster-global role cleanup no longer have a validation-to-dependency
window. Production and test migrations gain the same fix; no test-only serialization, role
renaming convention, dependency, or configuration is added.

An incompatible pre-existing role receives a provisional schema grant inside the failing
transaction. Rollback removes it before any other transaction can observe it. Migration error text,
required role attributes, canonical names, and final privileges remain unchanged.

## Considered & rejected

- **Serialize fixture lifetimes.** Test-only coordination can hide the race but leaves production
  migrations vulnerable when databases share a cluster. Filesystem locks also cannot coordinate
  concurrent runs using different per-run roots.
- **Use a maintenance-database advisory lock.** Test fixtures can open a second connection, but
  migration SQL cannot assume access to a particular maintenance database in production.
- **Rename runtime roles per worker.** That makes authority tests exercise names and assembly unlike
  production and does not fix the migration's false cross-database safety claim.
- **Validate and then grant, with a shorter window.** The reproduced drop needs only one schedulable
  point. Reducing the window does not establish an ordering invariant.
- **Do nothing.** One full suite already failed with 92 setup errors, and the deterministic
  two-database reproduction now exercises the same `UndefinedObject` at `GRANT`. Retaining the race
  violates the issue's deterministic migration criterion.
