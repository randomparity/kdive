# 0528 — Artifact ownership triple uniqueness

## Status

Accepted

## Context

Worker handlers write objects outside transaction-scoped advisory locks, then register catalog
rows under the lock (ADR-0519). Their phase-3 probe currently prevents duplicate catalog rows only
while every producer uses the same lock scope and transaction boundary. Duplicate claims are
unsafe because row-driven reclaim can delete an object while another row still claims it.

The existing partial unique index covers only investigation-owned object keys. The general
`object_key` btree is not a uniqueness constraint.

## Decision

Migration 0094 creates a unique index on `(owner_kind, owner_id, object_key)` for all artifact
owners. Historical duplicates make the migration fail for operator repair; the migration does not
choose or delete durable claims.

Worker phase-3 registration uses `INSERT ... ON CONFLICT DO NOTHING`. A conflict is an expected
concurrent outcome: the handler loads the winning row and follows the same compensation and
stat-based etag-repair path used when its phase-3 probe finds an existing claim. The probe remains
as an optimization and for clear control flow, while the index is the authoritative invariant.

## Consequences

- A future artifact producer cannot create a second catalog claim for the same ownership triple,
  even if it uses a different advisory-lock scope.
- A losing worker insert does not leak a raw integrity error or overwrite the winning row's etag.
- Deployment stops on historical duplicates instead of performing an implicit destructive repair.
- Object keys may still be reused by different owners; cross-owner key uniqueness is not claimed.

## Considered & rejected

- **Keep the application probe only.** This preserves the unenforced lock-scope precondition that
  caused #1750.
- **Let the unique violation retry the job.** The PUT has already occurred, and a generic retry
  bypasses the handlers' established claim compensation and etag reconciliation.
- **Upsert the losing attempt's etag.** Object-store write order and advisory-lock acquisition order
  differ, so the loser cannot safely declare its own etag authoritative.
- **Limit the index to today's worker owner kinds.** Duplicate ownership triples violate the same
  row-driven lifecycle invariant for every producer; enumerating kinds creates another omission
  hazard without preserving a valid use case.
