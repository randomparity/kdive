# Release finalize leaves an adopted cleanup receipt in place (#3435)

Decision record: [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md), amended
2026-10-30 by this change.

## Problem

When release cleanup finalization fails after its terminal journal record, the authority
publishes the private cleanup quarantine receipt as an `external_boot_recovery_quarantine` row.
A platform administrator may then resolve it with `ops.resolve_recovery_orphan` and the `adopt`
disposition. The provider marks the receipt `managed=True`, and the row moves to `adopted`.

A later attempt of the same release job still calls the adapter's `finalize`. Both providers
then remove the adopted receipt:

- Remote `finalize` (`remote_libvirt/external_boot_authority.py`) checks the operation
  identity, attempt and journal position, then calls `finalize_cleanup_quarantine`. It never
  reads `receipt.managed`.
- Local `finalize_cleanup_tombstone` (`local_libvirt/lifecycle/boot/external_boot.py`) calls
  the store's `finalize_tombstone`, which unlinks the receipt and the tombstone without reading
  the receipt.

Every other path already honors `managed`. That covers cleanup quarantine inventory, the
object-port observation, and the recovery-object `delete` on both providers. So the database
says `adopted` while the provider has removed the object. An automatic retry silently undoes an
audited administrator decision.

## Decision

Adoption protects the record. Release `finalize` makes no change when the standing cleanup
receipt is `managed`. Both providers behave the same way. The ADR-0787 amendment records this
and the rejected alternative, which is to keep removal and warn on adopt.

## Design

- **Remote.** In `RemoteExternalBootAuthorityAdapter.finalize`, after reading the
  receipt, return when `receipt.managed`, before the proof comparison. The adapter will not
  touch an operator-owned receipt, so a proof mismatch would guard nothing. Raising would also
  fail every later retry, because nothing un-adopts a receipt.
- **Local.** In `LocalLibvirtExternalBoot.finalize_cleanup_tombstone`, keep the existing proof
  argument check. Then, under the same pinned session, read the cleanup quarantine receipt and
  return when it is `managed`. Otherwise call `finalize_tombstone` as today. The store-level
  `finalize_tombstone` stays unchanged, because System teardown's
  `discard_system_teardown_recovery` also calls it and teardown owns destruction (ADR-0583).
- No schema, protocol, SQL, or service change. `_finalize_adapter` already treats a normal
  return as success. Quarantine publication already reports nothing for a managed receipt, so a
  no-op finalize publishes no second row.

## Failure model

1. **Actors and deployments.** These callers reach the changed code:
   - the provider authority service on a local-libvirt or remote-libvirt host, running a
     release job's CLEANUP `finalize`;
   - a platform administrator who earlier adopted the receipt through
     `ops.resolve_recovery_orphan`.
2. **Invariants and assets at stake.**
   - An adopted receipt, and the records it proves, stay on disk until an operator acts.
   - An unmanaged receipt is still finalized exactly as before, including idempotent repeat and
     post-delete absence.
   - The `adopted` row status matches provider state.
3. **Accepted failure classes.**
   - Adopted residue stays on the provider host indefinitely. Accepted: that is what adoption
     means. The same state already exists when no retry follows an adopt.
   - A remote finalize that was interrupted after its marker leaves a marker beside an adopted
     receipt. Accepted: the object port and inventory already report the receipt on disk,
     whatever the marker says (ADR-0787, #3326 amendment).
   - A local finalize that runs concurrently with an adopt is not covered. Accepted: both run
     under the System authority lane (ADR-0615), so they are serialized.
4. **Covered elsewhere.**
   - Inventory identity of the receipt: #3434.
   - Resolver adoption semantics beyond this change: out of scope (operator exclusion).
   - System teardown of adopted residue: ADR-0583 teardown authority, unchanged here.

## Success

1. ADR-0787 carries a self-contained 2026-10-30 amendment that states the decision and its
   rejected alternative.
2. Remote: after `adopt_recovery_object`, `finalize` with the matching context leaves the
   receipt `managed`, the recovery record readable, and no finalized marker written. Without
   adoption, the existing finalize tests still pass.
3. Local: after `adopt_object`, `finalize_cleanup_tombstone` leaves the tombstone directory and
   the managed receipt in place. Without adoption, the existing finalize tests still pass.

## Validation

- Success 2: `focused-test`, in
  `tests/providers/remote_libvirt/test_external_boot_authority.py`, named
  `test_remote_finalize_leaves_an_adopted_receipt_in_place`. Before the fix it fails because
  the recovery point is gone (`FileNotFoundError`).
- Success 3: `focused-test`, in `tests/providers/local_libvirt/test_external_boot.py`, named
  `test_real_adapter_finalization_leaves_an_adopted_receipt_in_place`. Before the fix it fails
  because the tombstone directory no longer exists.
- Success 1: `task-test-not-applicable`. The ADR is prose with no executable consumer. Its
  record shape is checked by `just records`.
