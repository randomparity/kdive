# ADR-0601: Reconstruct and finish cleanup tombstones

## Status

Accepted

## Context

Local external-boot cleanup replaces an owner-bound recovery intent with a v1 tombstone. The
tombstone already carries the complete `RecoveryPoint`, but the authority adapter retains the
point needed by finalization only in memory. A process restart therefore leaves an authenticated,
terminal cleanup unable to delete its tombstone. Teardown uses the same destructive primitive but
does not participate in finalization.

Publication has a second crash boundary: `tombstone.json` becomes durable before `intent.json` and
`preparation-result.json` are removed. The existing finalizer correctly refuses extra directory
content. A continuation must distinguish those exact producer-owned records from foreign content
without turning finalization into recursive cleanup.

## Decision

Both `cleanup` and `teardown` resolve their recovery point from the exact canonical v1 tombstone
when the intent is absent. After the authority service anchors the operation's terminal record, the
adapter reconstructs the point again, validates the request's plan and source/target identities,
builds the existing `FinalizeCleanupProof` from the anchored mutation-started context, and invokes
the existing descriptor-relative finalizer. No in-process pending-finalization map is authoritative.

`cleanup_complete` remains a read-only predicate, including when a tombstone has residual content.
Only `finalize_tombstone`, after validating the anchored finalization proof, may continue an
interrupted publication. When a matching tombstone is accompanied only by canonical `intent.json`
or `preparation-result.json`, it validates every present record against the tombstone's binding,
plan, materialization, recovery reference, and source/target identities. It then removes only those
two literal filenames and continues the existing tombstone finalization. Every retry repeats the
same comparisons. A malformed record, symlink, non-private inode, mismatched identity, or any other
directory entry fails closed without removing anything.

The v1 tombstone schema does not change, so no data migration is introduced.

## Consequences

- An authority-process restart no longer strands a valid cleanup or teardown tombstone.
- A crash during tombstone publication is resumable at each file-removal boundary, but only through
  authenticated finalization; observations remain read-only.
- Finalization still requires the service-supplied, still-current terminal replay path and its
  anchored mutation-started context; a tombstone alone grants no deletion authority.
- Recovery directories with unexplained content remain quarantined for operator inspection.
- Existing v1 tombstones remain readable without rewriting persisted data.

## Considered & rejected

- **Persist a second pending-finalization journal in the adapter.** judgment: the tombstone already
  contains the complete point, so a second store creates reconciliation and ordering states without
  adding authority.
- **Make finalization accept only a binding and trust whatever tombstone it finds.** verified:
  `CleanupTombstoneV1` carries a point digest while `RecoveryMetadataStore.finalize_tombstone`
  compares an independently supplied complete `RecoveryPoint`; dropping that comparison weakens
  the existing substitution fence (source at `a9e362435`).
- **Recursively delete every entry beside a valid tombstone.** judgment: unknown content is not
  proven producer residue and must remain fail-closed rather than becoming deletion scope.
- **Continue residual removal inside `cleanup_complete`.** verified: `_observe` calls
  `cleanup_is_accounted`, which delegates to `cleanup_complete`; mutating there would let an
  observation unlink durable evidence without an anchored commit context (source at `a9e362435`).
- **Leave restart cleanup to a later sweeper.** verified: issue #2244 records that no reconciler or
  sweep owns these tombstones, while terminal replay already calls the adapter finalizer with the
  authenticated context needed to finish them.
