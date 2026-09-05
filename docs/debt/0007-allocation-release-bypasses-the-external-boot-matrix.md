# 0007 — `allocations.release` ends a System's lease without consulting the matrix

## Status

> **Resolved by #2203** (2026-09-04)

## Concern

The external-boot admission matrix (ADR-0583) arbitrates every operation against the
activation restricting a System. `allocations.release` is not one of them, and the path it
takes cannot reach the matrix at all.

`src/kdive/mcp/tools/lifecycle/allocations/lifecycle.py` resolves the Allocation, checks
project membership and the `CONTRIBUTOR` role, and calls `release_with_backstops`.
`_release_locked` (`src/kdive/services/allocation/release.py`) branches only on
`AllocationState`: `released` is an idempotent ok, `expired`/`failed` are `stale_handle`,
`requested` releases directly, and `granted`/`active` go through `releasing -> released` with
the accounting credit. There is no read of the System, no teardown precondition, and no
activation lookup. The `precondition` hook that does re-check "no live System" exists only on
`reclaim_under_lock` and is used by the reconciler's orphaned-active reaper, not by the tool.
`docs/guide/reference/allocations.md` documents the tool as a final cleanup step a caller may
issue at any point, with teardown described as reconciler follow-up rather than a
prerequisite.

So a project contributor can release the Allocation of a System whose external boot is
`preparing` or `active` — ending the lease that authorizes the System while the guest still
runs the externally-booted kernel and its recovery point is still staged — and the matrix,
whose whole purpose is to arbitrate that, is never consulted.

The reviewer confirmed the missing precondition directly, by reading `_release_locked`
end to end. The reviewer did **not** trace what the reconciler does with a released
Allocation whose System still exists, so the blast radius beyond "the matrix is not
consulted" is inferred rather than demonstrated. That untraced gap is itself part of the
concern: the exemption that let this tool ship unguarded asserted the ordering was safe
without the trace that would establish it.

Exposure today is nil in the sense that nothing on this branch creates an activation, so no
System can be restricted; the interleaving becomes reachable when #2118 lands the activation
lifecycle.

## Why deferred

Guarding the tool is a new operation in a subsystem #2117's frozen surface does not name.
The matrix has no `ALLOCATION_RELEASE` member, the allocation service holds no `SYSTEM` lock
today and would need one taken in the ADR-0583 co-hold order, and deciding what the matrix
should answer — deny outright, or admit as the wind-down the ADR treats teardown as — is a
design question ADR-0583 does not settle for a lease-layer operation.

Answering it needs the lifecycle to exist. Until an activation can be created, the harmful
interleaving cannot be constructed, the reconciler behaviour that bounds the blast radius
cannot be observed, and any guard written now would be written against a hypothesis. The
change that makes the interleaving reachable is the change that should close this.

## Non-regression boundary

- The `allocations.release` entry in `_UNGUARDED_TOOLS`
  (`tests/services/external_boot/test_admission.py`) must keep stating that the tool is
  unguarded and that the matrix is not consulted. It must not be re-justified by an ordering
  the code does not enforce; the inverted registry gate in that module keeps the entry
  present and non-empty, but only a reader keeps it truthful.
- `reclaim_under_lock`'s `precondition` hook and the reconciler's orphaned-active reaper must
  stay in force while this record is open. They are the only place a live System is checked
  before a lease ends, and they are the bound on this gap.
- No new caller may reach `_release_locked` on a lease-holding path without either taking the
  `SYSTEM` lock or being recorded here, or the gap widens silently.

## What would resolve it

With #2118's activation lifecycle in place, trace a released Allocation whose System still
carries an uncleaned activation — what the reconciler does with it, and whether the System
becomes reachable by another tenant — then decide the matrix's answer for the lease layer.
If it is a denial, add an `ExternalBootOperation` member for the release, take the `SYSTEM`
lock on the Allocation's System in the ADR-0583 co-hold order, and guard `_release_locked`;
if it is an admission, say so in ADR-0583 with the reasoning and move the tool into the
guarded map.

Done when releasing the Allocation of a System restricted by an uncleaned external-boot
activation either returns the matrix's decision or is documented in ADR-0583 as admitted,
with a test covering the case that is chosen.

## Provenance

target: src/kdive/services/allocation/release.py
target: src/kdive/mcp/tools/lifecycle/allocations/lifecycle.py
target: tests/services/external_boot/test_admission.py
Found by the `$gauntlet` adversarial review of the #2117 branch on 2026-09-02 (finding 5 of
8). The exemption reason it challenged was corrected in that fix; the underlying gap it
describes is this record.
tracker: #2118

## Resolution

ADR-0596 adds allocation release to the closed matrix as a fail-closed operation. Both the
project-facing release and the reconciler's `reclaim_under_lock` path now acquire every historical
System lock in stable order and consult the matrix before ending the Allocation lease.
