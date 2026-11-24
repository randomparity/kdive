# 0855 — Bound external-boot repair admission to durable phase evidence

## Status

Accepted (2026-11-23)

Builds on [ADR-0839](0839-external-boot-repair-keyset-progress.md), retaining its lane cursors,
and [ADR-0596](0596-allocation-release-waits-for-external-boot-cleanup.md).

## Context

The newest source-job query includes automatic repairs. After a repair exhausts its lease
without committing progress, its job ID becomes the next source and creates a fresh dedup key.
Real Postgres repeats three admissions for one unchanged prepared activation. The queue already
uses NEVER recycling; resetting terminal rows is not the cause.

## Decision

Exclude the reserved `external-boot:repair:` dedup namespace only from source selection, before
its per-activation ranking and 100-source bound. Keep automatic jobs in current-authority live
suppression. Public producers use other namespaces; preserve their validation and authorization.

Extend the internal candidate with activation state, current recovery attempt UUID and persisted
phase readiness deadline. Activation uses its activation deadline; recovery uses its current
attempt deadline; release and cleanup use no deadline. These are lifecycle facts, unlike claim
owners, authority generations, leases, sweep time or updated timestamps.

An automatic admission key retains activation UUID, operation and original non-automatic source
UUID, plus a SHA-256 of an unambiguous JSON array containing phase facts and current authority
route. Encode deadlines as UTC timestamps. Retain the `external-boot:repair:` key namespace and
`repair:` opaque marker identity, keeping the marker within its existing 255-byte limit.
One tuple gets one durable queue slot with existing NEVER recycling and configured attempts.
Exhaustion without a new source or phase/route evidence does not replenish that slot.

A new explicit source job, committed phase evidence or changed authority route permits another
finite slot. An idempotent replay remains the same source. Recycling a direct request retains
its existing explicit attempts; it does not promise a new automatic slot. Preserve lane predicates,
keysets, lookup timeouts, transactions, payload checks and worker/authority fences.

## Consequences

Reconciler sweeps cannot create an unbounded chain from their own output. Missing retained source
still fails closed. Existing automatic jobs remain executable and live suppressors; none is deleted.
The new key format can admit one finite slot after upgrade for old evidence. This pre-release
change adds no compatibility reconstruction, persisted column or applied SQL modification.
Exhaustion requires an operator request or real evidence change, rather than automatic renewal.

## Considered & rejected

- **Filter automatic sources after fetching the ranked page.** verified: `_candidate_jobs`
  currently limits the newest 100 per activation, so a longer repair chain can hide its origin.
- **Use just the original UUID forever.** judgment: retain new opportunity after committed
  lifecycle progress or an operator authority-route change without making sweep bookkeeping count.
- **Include authority generation or updated_at in the budget.** verified: existing authority/worker
  operations change these fields without completing a lifecycle phase; they would renew attempts.
- **Add a new queue generation table or change direct-request recycling.** judgment: existing
  immutable request UUIDs, phase fields and durable dedup slots suffice for this repair.
