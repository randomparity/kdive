# 0754 Receipt-bound authority System discharge

## Status

Accepted (2026-10-12)

## Context

An authority System provision job can exhaust its attempts with an acknowledged
journal head but no terminal receipt. Migration 0149 preserves receipt ownership;
generic repair excludes the job and ordinary claiming cannot exceed its budget.
The supported `systems.teardown` operation can supersede that attempt, but its
absence receipt currently finishes only the teardown job. Allocation admission
also treats `torn-down` ownership as preactivation-fenced.

## Decision

Use the existing explicit `systems.teardown` operation to discharge exhausted
provisioning. Do not grant additional provision attempts or synthesize a provision
receipt. The provider authority retains ownership of quiescence, provider absence
and the terminal preactivation-absence receipt.

After that exact receipt is consumed and both ownership and System are torn down,
a bounded reconciler SQL repair cancels the matching expired, exhausted provision
job. It binds the immutable marker to the same ownership, excludes live leases,
active attempts and unconsumed terminal receipts, and retains the successor receipt
digest as its result reference. Old rows already discharged by teardown are eligible;
new teardown completion is not a prerequisite to repairing existing residue.

Only allocation release gains a receipt-proven preactivation exception. Other
mutation fences remain unchanged. The server and reconciler may read the boolean
proof through a restricted database function; raw authority tables remain private.
A missing, mismatched or unconsumed receipt does not admit release or cancel a job.

Migration 0171 adds the two restricted functions. Existing claim/count predicates,
attempt budgets, authority provider routes and generic dead-letter exclusions remain.
The operator sequence is `systems.teardown`, poll its job/System to `torn_down`,
then `allocations.release`; the reconciler settles the exhausted predecessor.

## Consequences

This is an operator-selected destructive discharge, not automatic provision retry.
Every allocation-release caller shares the admission, so after the receipt the
reconciler's lease-expiry sweep and orphaned-allocation reaper may also end the
Allocation; neither can destroy a System or invent its absence.
An unavailable authority or an unresolved provider conflict still blocks teardown;
no absence is invented. Restoring the existing authority and repeating the supported
teardown operation is required. Native POWER qualification remains separate.

## Considered & rejected

- **One extra acknowledged claim.** judgment: it adds another budget and consumption
  contract but still needs an operator discharge if that final grant is lost.
- **Repeat a grant for each new acknowledged head.** verified: migration 0165 bounds
  the external-boot precedent because repeated no-mutation heads otherwise recycle
  exhausted budgets. This decision adds no grant.
- **Generic dead-lettering or direct SQL cleanup instructions.** verified: ADR-0623
  reserves terminal evidence and provider absence to the authority. Neither an
  expired lease nor operator-edited rows prove absence.
- **Allow every mutation after torn-down ownership.** judgment: only allocation
  release is necessary; a general admission change would widen unrelated behavior.

## Amendment (2026-10-28): release after an activated System's teardown

The release exemption above covers a never-activated System, whose consumed absence receipt proves
it gone. An activated System ends its teardown with the ownership row `torn-down` and its first
activation recorded, and the pre-activation fence (`ownership_state != "activated"`) still
applied to it, so `allocations.release` was refused with
`authority_system_preactivation_mutation_fenced` after a clean teardown and the allocation's
capacity stayed held (#3382). Allocation release is now also admitted for a `torn-down` owner that
has an activation row; the first activation is claimed in the transaction that creates the
activation, so that row is the activation history. A claim blocked by a requested teardown can leave an unclaimed activation row
behind; while that row is not cleaned the activation policy already denies release, so the
proxy does not admit it. Reading `first_activation_id` directly would need a function that
exposes it, and is left to a later change. Every other operation keeps the fence, and a
never-activated `torn-down` owner still needs the receipt. No schema change.
