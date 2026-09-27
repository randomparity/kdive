# 0700 — Audit column grants in the worker catalog guard

## Status

Accepted (2026-09-27). Amends [ADR-0653](0653-worker-grant-catalog-guard.md)'s
table-privilege predicate for direct audit INSERT only. Issue: #2712.

## Context

Migration 0158 intentionally replaces broad audit INSERT with eight column grants.
The existing catalog guard rejects that authority because its direct route checks
only has_table_privilege. The inventory's seven audit writes use the same columns.

## Decision

For direct public.audit_log INSERT entries, require has_column_privilege for all
of principal, agent_session, project, tool, object_kind, object_id, transition,
and args_digest. Keep table predicates for other direct writes and existing
SECURITY DEFINER checks. Keep the inventory format unchanged. Prove the audit
predicate fails when one required column grant is revoked.

## Consequences

Least-privilege audit writes satisfy the guard; incomplete column authority fails.
The explicit column list must continue to match the two audit writers. Real-role
DB tests separately reject excluded columns and exercise both writers.

## Considered & rejected

- **Keep table-only checks.** verified: the first local pre-push gate for #2712
  failed test_declared_worker_writes_are_covered_by_migrated_catalog with seven
  missing audit INSERT entries after migration 0158; other role tests passed.
- **Use any-column authority.** judgment: one writable column cannot prove an eight-column write.
- **Add inventory column schema.** judgment: unnecessary schema and parser surface for one table.
