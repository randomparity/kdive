# Audit INSERT column authority (#2712)

## Scope and authority

The approved campaign scope for #2712 restricts worker and reconciler audit writes
without changing their writers. Exclusions are explicitly empty (operator approved
2026-09-27). Apply migration 0158 under [ADR-0015](../../adr/0015-sql-migration-runner.md).
No new architectural choice or caller migration is needed.

## Design

Revoke table-wide INSERT on public.audit_log from kdive_worker and kdive_reconciler.
Grant INSERT on principal, agent_session, project, tool, object_kind, object_id,
transition, args_digest to both roles. Preserve SELECT(id). Existing id/ts defaults
and nullable reason remain unchanged. The migration runner applies the revocation
and grant atomically; existing rows are unchanged. Existing deployments upgrade
through the same forward migration as new installations. Any corrective change
requires a later forward migration, never editing applied SQL.

record and record_system already name these eight columns. record_denial names
reason and is called by MCP denial middleware, image-tool denial handling, and
services.images.upload's quota denial path; the latter is imported only by the
image tool/registrar. These are server paths, with unchanged server grants.

## Failure model

A missing revocation leaves broad privileges effective; real-role forbidden INSERT
attempts must fail. An incomplete grant breaks normal audit writing; exercise both
existing async writers. Lost defaults or SELECT(id) break INSERT RETURNING; verify
persisted id/ts and readable id. Operator-added role memberships or superuser
connections are outside the runtime-role deployment proved here; tests use ordinary
LOGIN principals inheriting exactly one runtime role. No new runtime recovery path.

## Success and verification

Both runtime roles insert all eight permitted columns, receive a default UUID and
timestamp, and leave reason NULL. Naming id, ts, or reason fails with
InsufficientPrivilege, even when supplying DEFAULT. Effective writable columns
are exactly the eight named above; readable columns remain exactly id.
Existing worker record and reconciler record_system proofs continue to pass.
Migration discovery/tail expectations include 0158; the table privilege matrix no
longer expects table-wide audit INSERT. Run focused real Postgres tests with Docker
required, whole-tree lint/type, and the mandatory pre-push CI gate. VM tiers do not
exercise this database-only boundary and are not required.
