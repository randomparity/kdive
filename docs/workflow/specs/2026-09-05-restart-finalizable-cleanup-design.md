# Restart-finalizable local external-boot cleanup

## Goal and scope

Issue #2244 makes local external-boot cleanup and teardown finish their exact durable tombstones
after an authority-process restart. It also resumes the bounded crash state where a canonical
tombstone is durable beside producer-owned intent or preparation evidence. Production composition,
general storage reclamation, remote providers, readiness, and native ppc64le execution remain out
of scope.

[ADR-0601](../../adr/0601-reconstruct-and-finish-cleanup-tombstones.md) records the persistence and
deletion decision. The design uses the existing v1 tombstone and introduces no migration.

## Current failure

`LocalExternalBootAuthorityAdapter.finalize` accepts only `CLEANUP` and takes the point from
`_pending_cleanup_finalization`. A fresh adapter has an empty map, so terminal replay returns while
the durable tombstone remains. `TEARDOWN` publishes the same tombstone but never records or finalizes
it. Meanwhile `publish_tombstone` durably replaces `tombstone.json` before unlinking intent and
preparation evidence; `cleanup_complete` sees the tombstone and returns true, but the finalizer
refuses the extra entries.

## Invariants

1. A tombstone is durable evidence, not deletion authority. The authority service must first
   authenticate the request, prove it current, replay or anchor a matching terminal record, and
   supply the anchored mutation-started context to `finalize`.
2. Cleanup and teardown share the same tombstone resolution and finalization behavior.
3. Resolution reconstructs the complete `RecoveryPoint` only from a canonical tombstone whose
   binding and recovery reference match the requested activation.
4. Before finalization, the adapter compares request plan, source, and target identities against the
   reconstructed point and requires the request to name the recovery object it destroys.
5. Interrupted publication may remove only literal `intent.json` and
   `preparation-result.json`. Every present record must be canonical, private, non-symlinked, and
   identity-equivalent to the tombstone. Unknown entries cause zero removal.
6. Removing either known residual is independently crash-safe. A retry accepts the remaining valid
   subset, revalidates it, removes it, and fsyncs the directory.
7. Finalization remains descriptor-relative and idempotent. Absence succeeds only on the existing
   authenticated terminal replay path.
8. No provider mutation repeats merely to reconstruct or finalize a receipt.

## Components and flow

### Store continuation

`RecoveryMetadataStore.cleanup_complete(reference, recovery)` opens the owner-derived directory,
reads the exact tombstone, and constructs the expected tombstone from the caller's recovery point.
After equality succeeds it inventories the directory. The allowed set is `tombstone.json` plus any
subset of `intent.json` and `preparation-result.json`; any other name rejects before unlinking.

If intent is present, it is decoded with the existing canonical/private-file reader as
`LocalRecoveryMetadataV1`. A `RecoveryPoint` reconstructed from it must equal the tombstone point,
and its phase must be `recovered`. If preparation evidence is present, the existing canonical
receipt decoder validates it. Every contained receipt must match the tombstone binding and plan;
its materialization identity must match; a prepared receipt must carry the exact tombstone recovery
point. Validation of all present records completes before either unlink. The store then unlinks
only the two known names that were present, fsyncs the directory, reopens the tombstone, and returns
true. A crash after either unlink is safe because the remaining subset is accepted only after the
same validation on retry.

`finalize_tombstone` remains unchanged: it accepts only a directory containing the exact tombstone,
then removes that file and directory with the existing fsync sequence.

### Adapter reconstruction

The adapter treats both members of `_DELETING_OPERATIONS` alike. Commit and observation allow the
cleanup-receipt fallback for `CLEANUP` and `TEARDOWN`. `_apply` calls the shared cleanup primitive;
the durable tombstone replaces the in-memory pending map.

After the authority service has anchored a terminal record, `finalize` ignores non-deleting
operations. For cleanup or teardown it resolves the point through the normal intent-first,
tombstone-second lookup, applies `_require_matching_identities`, verifies named recovery ownership,
and calls `finalize_cleanup_tombstone` with `_cleanup_proof(context, point)`. Missing, malformed, or
mismatched state becomes the existing bounded `provider_conflict`; it never triggers cleanup or
deletion.

Terminal replay follows the same path, so an adapter created after process restart finishes the
same receipt without repeating the provider mutation. A retry after finalization reaches the
existing exact post-delete idempotence contract only while the authority service can replay the
matching terminal record and started context.

## Error handling

- Canonical decode, ownership, permission, symlink, or directory-content failures remain local and
  map through the adapter to `provider_conflict`; host paths and exception text do not cross the
  authority protocol.
- An absent tombstone cannot independently prove completion. It is accepted only by the existing
  finalizer after the authority service has selected a matching terminal operation and supplied its
  started-record context.
- Validation precedes removal. A mixed or foreign directory is unchanged even when one record is
  otherwise valid.
- No recursive delete, glob, path from peer input, schema fallback, or record repair is introduced.

## Threat model

### Boundary inventory

- **Existing boundary widened:** authenticated authority requests can now cause teardown, as well
  as cleanup, to finalize local recovery evidence.
- **Existing boundary widened:** durable tombstone content can now replace the adapter's in-memory
  point after restart.
- **Existing boundary widened:** canonical producer-owned intent and preparation evidence can be
  removed during interrupted-publication continuation.

### Actors and trust

Authenticated tenants control request identity fields and recovery-object lists but not the
authority journal, configured recovery root, or descriptor-relative filenames. A local process or
operator may alter recovery-directory content; such content is untrusted until canonical parsing,
ownership checks, and inode constraints succeed. The authority service and its anchored journal
context are trusted to establish current terminal execution.

### Controls

| Boundary | Validation and authorization | Bound and destination safety | Failure disclosure |
| --- | --- | --- | --- |
| request to finalizer | service authentication/current-generation/terminal replay; exact operation, request identities, and named recovery object | one owner-derived directory and one tombstone | closed `provider_conflict` only |
| tombstone to point | canonical closed model; exact binding, reference, digest, plan, source, target, and materialization identities | one bounded v1 record read via `O_NOFOLLOW` | exception details remain host-local logs |
| residual to unlink | complete preflight inventory; canonical private-file reads; all identities match before mutation | two literal filenames, no recursion or peer path | closed provider category |

### Out of scope

Compromise of the authority service or configured recovery-root owner is outside this change; those
are existing trusted components. General orphan discovery and retention policy belong to #2245.
Remote-libvirt storage has separate provider contracts. Native ppc64le proof is excluded by the
campaign, while architecture-independent tests remain required.

## Verification

- Adapter tests cover cleanup and teardown first execution, restart reconstruction, terminal
  replay, before/after-finalization retries, request identity substitution, malformed receipts,
  and proof propagation. Removing the durable receipt fallback must strand a tombstone and fail a
  test.
- Real-store tests inject faults after tombstone publication and after each known residual removal.
  A fresh store/adapter completes exact states without a second provider cleanup.
- Real-store negative tests cover foreign binding, changed point/digest, stale operation identity,
  malformed JSON, symlink and mode violations, and an unknown entry; each asserts no removal.
- Focused local-libvirt tests, lint, type checking, and the full `just ci` gate must pass. No native
  ppc64le run is part of this issue.

## Operational proof

The behavior is a descriptor-scoped filesystem state machine and the production port is not yet
bound (#2246), so the acceptance signal is the real-store fault-injection suite using actual files,
permissions, links, fsyncs, and fresh adapter instances. A live VM cannot reach this dormant seam
before #2246 and would not add evidence beyond those filesystem operations.
