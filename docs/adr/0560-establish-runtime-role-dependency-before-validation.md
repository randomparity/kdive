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

Migration 0104 remains byte-immutable. Immediately before the migration runner executes a pending
0104, it establishes the current database's schema dependency for every existing canonical runtime
role. It attempts `GRANT USAGE ON SCHEMA public` for each name and ignores only
`undefined_object`, which means that role is not present. It then executes the unchanged migration.

The precondition and migration share the runner's migration transaction. If a role exists, the
grant creates the dependency before 0104 validates it. If a concurrent drop wins first, the grant
reports the role absent and 0104 follows its existing create-then-grant path; creation protects the
new shared-catalog row until its dependency exists. If a role is incompatible, 0104 raises its
existing error and PostgreSQL rolls back the provisional grant. An uncommitted grant is not visible
to other sessions.

The precondition runs only when 0104 is pending. A database that already recorded 0104 neither
replays it nor changes checksum behavior. The runner keeps the precondition adjacent to the exact
version check rather than introducing a general migration-hook framework.

The regression executes the runner precondition and migration 0104's real file bytes after
mechanically substituting an isolated set of runtime-role names; it neither copies nor reimplements
the migration algorithm. The precondition accepts the names as a narrow test seam, while production
calls it with the fixed canonical tuple.

Two fixed-path schedules cover both outcomes. In the drop-wins schedule, the cross-database drop
completes before the precondition grant; 0104 must recreate, grant, validate, and leave the exact
role shape and dependency. In the grant-wins schedule, the precondition establishes its dependency
before the drop starts; the drop must block and then fail with `DependentObjectsStillExist`. A
companion control skips the precondition, pauses unchanged 0104 after validation, and must reproduce
the reported `UndefinedObject` at the grant.

## Consequences

Parallel migrations and cluster-global role cleanup no longer have a validation-to-dependency
window when 0104 is pending. Production and test migrations gain the same fix; no test-only
serialization, dependency, configuration, or historical migration edit is added.

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
- **Edit migration 0104 in place.** ADR-0015 and the schema guard make applied migrations
  byte-immutable; changing it would fail existing database checksum validation.
- **Add only a forward migration.** A fresh database still has to execute vulnerable 0104 before it
  can reach a later repair migration.
- **Do nothing.** One full suite already failed with 92 setup errors, and the deterministic
  two-database reproduction now exercises the same `UndefinedObject` at `GRANT`. Retaining the race
  violates the issue's deterministic migration criterion.
